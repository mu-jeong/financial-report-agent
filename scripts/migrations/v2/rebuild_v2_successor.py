"""Safely replace an active Native V2 index with a full-corpus successor."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


@dataclass(frozen=True)
class ExtractionPolicy:
    primary: str
    fallback: str | None
    allow_fallback: bool
    profile: str


@dataclass(frozen=True)
class RebuildInspection:
    active_snapshot_id: str
    active_profile: str
    requested_profile: str
    active_report_count: int
    source_pdf_count: int
    profile_matches: bool


@dataclass(frozen=True)
class RebuildExecution:
    previous_snapshot_id: str
    active_snapshot_id: str
    active_profile: str
    report_count: int
    indexed_report_count: int
    extraction_failure_count: int


def load_config() -> Any:
    from src.configs import config

    return config


def configured_extraction_policy(config: Any) -> ExtractionPolicy:
    """Return the extraction policy used by Native V2 incremental updates."""

    from src.retrieval.build_service import format_extraction_profile

    main_engine = str(config.PDF_EXTRACTION_ENGINE or "").strip()
    override = str(config.UNEMBEDDED_PDF_EXTRACTION_ENGINE or "").strip()
    primary = override or main_engine
    configured_fallback = str(config.PDF_EXTRACTION_FALLBACK_ENGINE or "").strip()
    allow_fallback = bool(configured_fallback) and primary == main_engine
    fallback = configured_fallback if allow_fallback else None
    if fallback == primary:
        fallback = None
        allow_fallback = False
    profile = format_extraction_profile(
        primary,
        allow_fallback=allow_fallback,
        fallback_engine=fallback,
        allow_custom=True,
    )
    return ExtractionPolicy(primary, fallback, allow_fallback, profile)


def profile_matches_policy(active_profile: str, policy: ExtractionPolicy) -> bool:
    from src.retrieval.build_service import format_legacy_import_extraction_profile

    imported_profile = format_legacy_import_extraction_profile(
        policy.primary,
        allow_fallback=policy.allow_fallback,
        fallback_engine=policy.fallback,
        allow_custom=True,
    )
    return active_profile in {policy.profile, imported_profile}


def progress_extractor(policy: ExtractionPolicy, *, total: int) -> Any:
    """Return an extractor that keeps long full-corpus runs visibly moving."""

    from src.core.pdf_extraction import extract_pdf_text, normalize_engine

    normalize_engine(policy.primary)
    if policy.fallback:
        normalize_engine(policy.fallback)

    current = 0
    progress_output_available = True

    def extract(path: Path, engine: str) -> Any:
        nonlocal current, progress_output_available
        current += 1
        if progress_output_available:
            try:
                print(f"[PDF {current}/{total}] {path.name}", flush=True)
            except (OSError, ValueError):
                progress_output_available = False
        return extract_pdf_text(
            str(path),
            engine,
            clean=True,
            allow_fallback=policy.allow_fallback,
            fallback_engine=policy.fallback,
        )

    return extract


def _active_profile(selection: Any) -> tuple[str, int]:
    catalog = Path(selection.paths.catalog).resolve(strict=True)
    connection = sqlite3.connect(catalog)
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT profile.extractor, build.included_count
            FROM retrieval_builds AS build
            JOIN embedding_profiles AS profile
              ON profile.profile_id = build.profile_id
            WHERE build.build_id = ?
            """,
            (selection.active_build_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("active Native V2 embedding profile is unavailable")
    return str(row[0]), int(row[1])


def inspect_rebuild(config: Any) -> RebuildInspection:
    from src.retrieval.bootstrap import inspect_runtime

    selection = inspect_runtime(config.DATA_ROOT)
    if (
        not selection.is_native
        or not selection.active_snapshot_id
        or not selection.active_build_id
    ):
        raise RuntimeError(
            "Active Native V2 data is unavailable. Run RUN_QUICKSTART.bat first."
        )
    source_root = Path(config.SAVE_DIR).resolve(strict=True)
    if source_root.is_symlink() or not source_root.is_dir():
        raise RuntimeError("The PDF source directory must be a real local directory")
    policy = configured_extraction_policy(config)
    active_profile, active_report_count = _active_profile(selection)
    source_pdf_count = sum(
        1 for path in source_root.glob("*.pdf") if path.is_file() and not path.is_symlink()
    )
    return RebuildInspection(
        active_snapshot_id=str(selection.active_snapshot_id),
        active_profile=active_profile,
        requested_profile=policy.profile,
        active_report_count=active_report_count,
        source_pdf_count=source_pdf_count,
        profile_matches=profile_matches_policy(active_profile, policy),
    )


def _successor_kwargs(
    config: Any,
    policy: ExtractionPolicy,
    *,
    embeddings: Any,
    source_pdf_count: int,
) -> dict[str, Any]:
    return {
        "embeddings": embeddings,
        "model": config.EMBEDDING_MODEL,
        "extractor_name": policy.primary,
        "fallback_extractor_name": policy.fallback,
        "allow_extraction_fallback": policy.allow_fallback,
        "extractor": progress_extractor(policy, total=source_pdf_count),
        "use_parent_child": config.USE_PARENT_CHILD,
        "single_chunk_size": config.CHUNK_SIZE,
        "parent_chunk_size": config.PARENT_CHUNK_SIZE,
        "child_chunk_size": config.CHILD_CHUNK_SIZE,
        "metric": "l2",
        "normalization": "none",
    }


def execute_rebuild(
    config: Any,
    inspection: RebuildInspection,
    *,
    policy: ExtractionPolicy | None = None,
) -> RebuildExecution:
    from src.core.embed_pipeline import build_embeddings_fn
    from src.retrieval.build_service import execute_full_corpus_successor

    if inspection.source_pdf_count <= 0:
        raise RuntimeError("No source PDFs are available for the rebuild")
    data_root = Path(config.DATA_ROOT).resolve(strict=True)
    selected_policy = policy or configured_extraction_policy(config)
    result, outcome = execute_full_corpus_successor(
        data_root,
        config.SAVE_DIR,
        **_successor_kwargs(
            config,
            selected_policy,
            embeddings=build_embeddings_fn(),
            source_pdf_count=inspection.source_pdf_count,
        ),
    )
    return RebuildExecution(
        previous_snapshot_id=inspection.active_snapshot_id,
        active_snapshot_id=str(outcome.active_snapshot_id),
        active_profile=selected_policy.profile,
        report_count=result.report_count,
        indexed_report_count=result.indexed_report_count,
        extraction_failure_count=result.extraction_failure_count,
    )


def _print_inspection(inspection: RebuildInspection) -> None:
    print(f"Current snapshot: {inspection.active_snapshot_id}")
    print(f"Current extraction profile: {inspection.active_profile}")
    print(f"Requested extraction profile: {inspection.requested_profile}")
    print(f"Currently indexed reports: {inspection.active_report_count}")
    print(f"Source PDFs: {inspection.source_pdf_count}")


def _confirm_rebuild() -> bool:
    if not sys.stdin.isatty():
        return False
    return input("Type REBUILD to create a full Native V2 successor: ").strip() == "REBUILD"


def _current_snapshot_or_fallback(config: Any, fallback_snapshot_id: str) -> str:
    try:
        return inspect_rebuild(config).active_snapshot_id
    except Exception:
        return fallback_snapshot_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild active Native V2 data as a validated full-corpus successor"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        inspection = inspect_rebuild(config)
    except Exception as exc:
        print(f"[failed] Native V2 rebuild preflight failed: {exc}")
        return 2

    _print_inspection(inspection)
    if args.check:
        status = "matches" if inspection.profile_matches else "rebuild required"
        print(f"Profile status: {status}")
        return 0
    if inspection.profile_matches and not args.force:
        print("[complete] The active profile already matches the configured profile.")
        return 0
    if not args.yes and not _confirm_rebuild():
        print("[cancelled] No data was changed.")
        return 3

    try:
        execution = execute_rebuild(config, inspection)
    except KeyboardInterrupt:
        active = _current_snapshot_or_fallback(config, inspection.active_snapshot_id)
        print(f"[cancelled] Current active snapshot: {active}")
        return 130
    except Exception as exc:
        active = _current_snapshot_or_fallback(config, inspection.active_snapshot_id)
        print(f"[failed] Native V2 successor rebuild failed: {exc}")
        print(f"Current active snapshot: {active}")
        return 1

    print("[complete] A validated Native V2 successor is active.")
    print(f"Previous snapshot: {execution.previous_snapshot_id}")
    print(f"Current snapshot: {execution.active_snapshot_id}")
    print(f"Current extraction profile: {execution.active_profile}")
    print(f"Source PDFs: {execution.report_count}")
    print(f"Indexed reports: {execution.indexed_report_count}")
    print(f"Extraction failures/exclusions: {execution.extraction_failure_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
