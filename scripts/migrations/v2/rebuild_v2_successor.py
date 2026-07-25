"""Safely replace an active V2 index with a full-corpus successor."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

ENV_PATH = REPOSITORY_ROOT / ".env"
DEFAULT_PRIMARY = "pymupdf"
DEFAULT_FALLBACK = "opendataloader"


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
    publication_generation: int
    write_epoch: int


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _env_primary_conflict(path: Path) -> tuple[str, str] | None:
    values = _read_env_values(path)
    legacy = values.get("EXTRACTION_ENGINE", "").strip().lower()
    friendly = values.get("PDF_EXTRACTION_ENGINE", "").strip().lower()
    if legacy and friendly and legacy != friendly:
        return legacy, friendly
    return None


def _effective_env_value(
    values: dict[str, str],
    key: str,
    legacy_alias: str,
) -> str:
    raw = values[key] if key in values else values.get(legacy_alias, "")
    return raw.strip().lower()


def _known_reversed_default_policy(path: Path) -> bool:
    values = _read_env_values(path)
    legacy = values.get("EXTRACTION_ENGINE", "").strip().lower()
    primary = _effective_env_value(
        values,
        "PDF_EXTRACTION_ENGINE",
        "EXTRACTION_ENGINE",
    )
    fallback = _effective_env_value(
        values,
        "PDF_EXTRACTION_FALLBACK_ENGINE",
        "EXTRACTION_FALLBACK_ENGINE",
    )
    pending = _effective_env_value(
        values,
        "UNEMBEDDED_PDF_EXTRACTION_ENGINE",
        "UNEMBEDDED_EXTRACTION_ENGINE",
    )
    reversed_primary = (
        legacy == DEFAULT_PRIMARY
        and primary == DEFAULT_FALLBACK
        and fallback in {"", DEFAULT_PRIMARY}
    )
    historical_pending_default = (
        primary == DEFAULT_PRIMARY
        and pending == DEFAULT_FALLBACK
        and fallback in {"", DEFAULT_PRIMARY}
    )
    return reversed_primary or historical_pending_default


def _default_policy_is_configured(path: Path) -> bool:
    values = _read_env_values(path)
    return (
        _effective_env_value(
            values,
            "PDF_EXTRACTION_ENGINE",
            "EXTRACTION_ENGINE",
        )
        == DEFAULT_PRIMARY
        and _effective_env_value(
            values,
            "PDF_EXTRACTION_FALLBACK_ENGINE",
            "EXTRACTION_FALLBACK_ENGINE",
        )
        == DEFAULT_FALLBACK
        and _effective_env_value(
            values,
            "UNEMBEDDED_PDF_EXTRACTION_ENGINE",
            "UNEMBEDDED_EXTRACTION_ENGINE",
        )
        == DEFAULT_PRIMARY
    )


def _upsert_env_values(lines: list[str], updates: dict[str, str]) -> list[str]:
    written: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                if key not in written:
                    output.append(f"{key}={updates[key]}")
                    written.add(key)
                continue
        output.append(line)
    remaining = {
        key: value for key, value in updates.items() if key not in written
    }
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# V2 successor rebuild extraction policy")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    return output


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.v2-rebuild-{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def repair_known_reversed_default_policy(path: Path = ENV_PATH) -> bool:
    """Repair only the known legacy/new-key reversal shipped to existing users."""

    if not _known_reversed_default_policy(path):
        return False
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    updated = _upsert_env_values(
        lines,
        {
            "PDF_EXTRACTION_ENGINE": DEFAULT_PRIMARY,
            "PDF_EXTRACTION_FALLBACK_ENGINE": DEFAULT_FALLBACK,
            "UNEMBEDDED_PDF_EXTRACTION_ENGINE": DEFAULT_PRIMARY,
        },
    )
    _atomic_write_text(path, "\n".join(updated) + "\n")
    return True


def load_config() -> Any:
    from src.configs import config

    return config


def configured_extraction_policy(config: Any) -> ExtractionPolicy:
    """Return the exact policy used by future native incremental updates."""

    from src.retrieval.build_service import format_extraction_profile

    main_engine = str(config.EXTRACTION_ENGINE or "").strip()
    override = str(getattr(config, "UNEMBEDDED_EXTRACTION_ENGINE", "") or "").strip()
    primary = override or main_engine
    configured_fallback = str(
        getattr(config, "EXTRACTION_FALLBACK_ENGINE", "") or ""
    ).strip()
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
    return ExtractionPolicy(
        primary=primary,
        fallback=fallback,
        allow_fallback=allow_fallback,
        profile=profile,
    )


def default_extraction_policy() -> ExtractionPolicy:
    """Return the repository policy used to repair known historical defaults."""

    from src.retrieval.build_service import format_extraction_profile

    return ExtractionPolicy(
        primary=DEFAULT_PRIMARY,
        fallback=DEFAULT_FALLBACK,
        allow_fallback=True,
        profile=format_extraction_profile(
            DEFAULT_PRIMARY,
            allow_fallback=True,
            fallback_engine=DEFAULT_FALLBACK,
            allow_custom=True,
        ),
    )


def profile_matches_policy(active_profile: str, policy: ExtractionPolicy) -> bool:
    """Treat a matching V1 migration profile as the same extraction policy."""

    from src.retrieval.build_service import format_legacy_import_extraction_profile

    accepted_profiles = {
        policy.profile,
        format_legacy_import_extraction_profile(
            policy.primary,
            allow_fallback=policy.allow_fallback,
            fallback_engine=policy.fallback,
            allow_custom=True,
        ),
    }
    return active_profile in accepted_profiles


def progress_extractor(
    policy: ExtractionPolicy,
    *,
    total: int,
) -> Any:
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
                # Progress is diagnostic only. A detached or closed Windows
                # console must never turn a valid PDF into an extraction
                # failure.
                progress_output_available = False
        return extract_pdf_text(
            str(path),
            engine,
            clean=True,
            allow_fallback=policy.allow_fallback,
            fallback_engine=policy.fallback,
        )

    return extract


def _with_requested_policy(
    inspection: RebuildInspection,
    policy: ExtractionPolicy,
) -> RebuildInspection:
    return RebuildInspection(
        active_snapshot_id=inspection.active_snapshot_id,
        active_profile=inspection.active_profile,
        requested_profile=policy.profile,
        active_report_count=inspection.active_report_count,
        source_pdf_count=inspection.source_pdf_count,
        profile_matches=profile_matches_policy(inspection.active_profile, policy),
    )


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
        raise RuntimeError("active V2 embedding profile is unavailable")
    return str(row[0]), int(row[1])


def inspect_rebuild(config: Any) -> RebuildInspection:
    from src.retrieval.bootstrap import inspect_runtime

    selection = inspect_runtime(config.DB_PATH)
    if (
        not selection.is_native
        or not selection.active_snapshot_id
        or not selection.active_build_id
    ):
        raise RuntimeError(
            "활성 V2가 없습니다. 먼저 MIGRATE_V2.bat으로 V1 데이터를 V2로 전환하세요."
        )
    source_root = Path(config.SAVE_DIR).resolve(strict=True)
    if source_root.is_symlink() or not source_root.is_dir():
        raise RuntimeError("PDF 원본 폴더가 안전한 로컬 디렉터리가 아닙니다.")
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
    data_root: Path,
    embeddings: Any,
    source_pdf_count: int,
) -> dict[str, Any]:
    return {
        "data_root": data_root,
        "embeddings": embeddings,
        "model": config.EMBEDDING_MODEL,
        "extractor_name": policy.primary,
        "fallback_extractor_name": policy.fallback,
        "allow_extraction_fallback": policy.allow_fallback,
        "extractor": progress_extractor(
            policy,
            total=source_pdf_count,
        ),
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
        raise RuntimeError("재생성할 PDF 원본이 없습니다.")
    data_root = Path(config.DB_PATH).resolve(strict=False).parent.resolve(strict=True)
    selected_policy = policy or configured_extraction_policy(config)
    result, outcome = execute_full_corpus_successor(
        config.DB_PATH,
        config.SAVE_DIR,
        **_successor_kwargs(
            config,
            selected_policy,
            data_root=data_root,
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
        publication_generation=outcome.publication_generation,
        write_epoch=outcome.write_epoch,
    )


def _print_inspection(inspection: RebuildInspection) -> None:
    print(f"현재 snapshot: {inspection.active_snapshot_id}")
    print(f"현재 추출 프로필: {inspection.active_profile}")
    print(f"재생성 추출 프로필: {inspection.requested_profile}")
    print(f"현재 인덱싱 문서: {inspection.active_report_count}개")
    print(f"원본 PDF: {inspection.source_pdf_count}개")


def _confirm_rebuild() -> bool:
    if not sys.stdin.isatty():
        return False
    answer = input("전체 successor 재생성을 진행하려면 REBUILD를 입력하세요: ").strip()
    return answer == "REBUILD"


def _current_snapshot_or_fallback(
    config: Any,
    fallback_snapshot_id: str,
) -> str:
    try:
        return inspect_rebuild(config).active_snapshot_id
    except Exception:
        return fallback_snapshot_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild an active Finance LLM V2 index as a validated full-corpus successor"
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="inspect profiles and source counts without changing files or snapshots",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="perform the rebuild without the Python confirmation prompt",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild even when the active and configured profiles already match",
    )
    parser.add_argument(
        "--use-configured-policy",
        action="store_true",
        help="do not repair the known reversed default .env policy before rebuilding",
    )
    args = parser.parse_args(argv)

    known_default_mismatch = _known_reversed_default_policy(ENV_PATH)
    repair_planned = known_default_mismatch and not args.use_configured_policy

    conflict = _env_primary_conflict(ENV_PATH)
    if conflict is not None:
        print(
            "[설정 알림] legacy EXTRACTION_ENGINE과 PDF_EXTRACTION_ENGINE이 "
            f"다릅니다: legacy={conflict[0]}, effective={conflict[1]}"
        )
        if args.check and repair_planned:
            print("일반 실행 시 알려진 기본 정책 충돌만 자동 교정합니다.")

    try:
        config = load_config()
        inspection = inspect_rebuild(config)
        target_policy: ExtractionPolicy | None = None
        if repair_planned:
            target_policy = default_extraction_policy()
            inspection = _with_requested_policy(
                inspection,
                target_policy,
            )
    except Exception as exc:
        print(f"[실패] V2 재생성 사전 점검 실패: {exc}")
        return 2

    print("=" * 68)
    print("Finance LLM V2 full-corpus successor")
    print("=" * 68)
    _print_inspection(inspection)

    if args.check:
        if inspection.profile_matches and not repair_planned:
            print("[확인] 활성 프로필과 현재 설정이 일치합니다.")
        else:
            print(
                "[조치 필요] REBUILD_V2.bat을 실행하면 기존 V2를 유지한 채 "
                "새 successor를 생성합니다."
            )
        return 0

    if inspection.profile_matches and not args.force and not repair_planned:
        print("[완료] 활성 프로필과 현재 설정이 같아 재생성이 필요하지 않습니다.")
        return 0
    if not args.yes and not _confirm_rebuild():
        print("[중단] 확인되지 않아 아무 것도 변경하지 않았습니다.")
        return 3

    if repair_planned:
        repaired = repair_known_reversed_default_policy(ENV_PATH)
        if not repaired and not _default_policy_is_configured(ENV_PATH):
            print(
                "[실패] 점검 후 .env 추출 설정이 변경되었습니다. "
                "REBUILD_V2.bat --check를 다시 실행하세요."
            )
            return 2
        if repaired:
            print(
                "[설정 교정] 알려진 역방향 설정을 "
                "pymupdf -> opendataloader fallback으로 변경했습니다."
            )
    if inspection.profile_matches and not args.force:
        print("[완료] 설정을 교정했고 활성 프로필은 이미 같아 재생성이 필요하지 않습니다.")
        return 0

    print(
        "새 snapshot을 별도로 생성합니다. 실패하면 현재 활성 V2는 계속 유지됩니다."
    )
    print("전체 PDF 파싱과 임베딩 API 호출로 시간이 오래 걸릴 수 있습니다.")
    print(
        "PyMuPDF와 OpenDataLoader가 모두 실패한 PDF는 제외 상태로 기록하고 "
        "나머지 문서를 계속 처리합니다."
    )
    try:
        execution = execute_rebuild(
            config,
            inspection,
            policy=target_policy,
        )
    except KeyboardInterrupt:
        active = _current_snapshot_or_fallback(config, inspection.active_snapshot_id)
        print("\n[중단] 사용자 요청으로 재생성을 중단했습니다.")
        print(f"현재 활성 snapshot: {active}")
        return 130
    except Exception as exc:
        active = _current_snapshot_or_fallback(config, inspection.active_snapshot_id)
        print(f"\n[실패] V2 successor 재생성 실패: {exc}")
        if active == inspection.active_snapshot_id:
            print(
                f"기존 V2 snapshot은 그대로 유지됩니다: "
                f"{inspection.active_snapshot_id}"
            )
        else:
            print(f"현재 활성 snapshot을 확인하세요: {active}")
        return 1

    print("\n[완료] 검증된 V2 successor가 활성화되었습니다.")
    print(f"이전 snapshot: {execution.previous_snapshot_id}")
    print(f"현재 snapshot: {execution.active_snapshot_id}")
    print(f"현재 추출 프로필: {execution.active_profile}")
    print(f"원본 PDF: {execution.report_count}개")
    print(f"인덱싱 성공: {execution.indexed_report_count}개")
    print(f"추출 실패/제외: {execution.extraction_failure_count}개")
    print(
        "이전 snapshot은 rollback용 predecessor로 잠시 보존되지만 "
        "검색에는 사용되지 않습니다."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
