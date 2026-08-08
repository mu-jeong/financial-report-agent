"""One-click offline migration from a standard V1 install to Native V2.

The V1 SQLite/docstore/FAISS corpus is read only.  Its chunks and vector values
are translated into the current native catalog and published through the normal
writer/recovery protocol.  After a healthy writable native runtime is observed,
the obsolete V1 database and vector-store files are removed; downloaded PDFs
remain the authoritative source corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.migrations.v2.assess import ProvenanceEvidence, assess_v1_install
from src.migrations.v2.import_v1 import V1ImportResult, execute_v1_import
from src.retrieval.bootstrap import RuntimeSelection, inspect_runtime
from src.retrieval.build_service import (
    NativeBuildError,
    format_legacy_import_extraction_profile,
)
from src.retrieval.identity import EmbeddingProfile
from src.retrieval.initializer import initialize_empty_native
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from src.retrieval.update_lock import RetrievalUpdateLock
from src.retrieval.writer_lock import NativeWriterLock


V1_ARTIFACTS = (
    "reports.db",
    "vector_db/index.faiss",
    "vector_db/index.pkl",
)
V1_OPTIONAL_SIDECARS = ("reports.db-wal", "reports.db-shm", "reports.db-journal")
V1_CLEANUP_MARKER = "retrieval/v2/migration/v1-cleanup.json"
PREFIX_TEMPLATE = "[Company: {target_name}, Title: {title}]\n"


class MigrationError(RuntimeError):
    """Raised when V1 cannot be migrated without risking the active data."""


@dataclass(frozen=True)
class UserMigrationSettings:
    data_root: Path
    source_dir: Path
    model: str
    extractor: str
    parent_chunk_size: int
    child_chunk_size: int
    use_parent_child: bool
    fallback_extractor: str | None = None


@dataclass(frozen=True)
class MigrationOutcome:
    status: str
    snapshot_id: str
    publication_generation: int
    write_epoch: int
    write_enabled: bool
    vector_count: int
    max_vector_absolute_error: float
    removed_v1_artifacts: tuple[str, ...]


def migrate_v1_to_v2(settings: UserMigrationSettings) -> MigrationOutcome:
    """Publish the V1 vectors as Native V2, then retire the V1 artifacts."""

    normalized = _validated_settings(settings)
    current = _inspect(normalized.data_root)
    if current.is_native and not current.is_empty:
        _require_healthy_native(current)
        with RetrievalUpdateLock(normalized.data_root):
            with NativeWriterLock(normalized.data_root):
                current = _inspect(normalized.data_root)
                _require_healthy_native(current)
                if _has_v1_artifacts(normalized.data_root):
                    _require_cleanup_authorized(normalized.data_root, current)
                    removed = _retire_v1_artifacts(normalized.data_root)
                else:
                    removed = ()
                    _remove_completed_cleanup_marker(normalized.data_root, current)
        return _outcome(
            "already_migrated",
            current,
            vector_count=_active_vector_count(current),
            max_vector_absolute_error=0.0,
            removed_v1_artifacts=removed,
        )
    if current.mode not in {"uninitialized", "native"} or (
        current.is_native and not current.is_empty
    ):
        raise MigrationError(f"unsupported retrieval runtime: {current.mode}")

    legacy_paths = _validated_v1_artifacts(normalized.data_root)
    before_hashes = {
        relative: _sha256_file(path) for relative, path in legacy_paths.items()
    }
    source_hashes = _source_pdf_hashes(
        legacy_paths["reports.db"],
        normalized.source_dir,
    )
    provenance = ProvenanceEvidence(
        model=normalized.model,
        normalization="none",
        same_space_attested=False,
    )
    assessment = assess_v1_install(
        normalized.data_root,
        expected_hashes=before_hashes,
        provenance=provenance,
    )
    profile = _embedding_profile(
        normalized,
        dimension=assessment.observable.dimension,
        metric=assessment.observable.metric,
    )

    imported: V1ImportResult
    with RetrievalUpdateLock(normalized.data_root):
        with NativeWriterLock(normalized.data_root) as writer_lease:
            catalog = normalized.data_root / "retrieval" / "v2" / "catalog.sqlite3"
            if not catalog.exists():
                initialize_empty_native(
                    normalized.data_root,
                    writer_lease=writer_lease,
                )
            else:
                recovery = StartupReconciler(normalized.data_root).reconcile(
                    writer_lease=writer_lease,
                )
                if recovery.disposition == RecoveryDisposition.FAIL_CLOSED:
                    raise MigrationError(
                        f"native recovery failed before migration: {recovery.reason}"
                    )
            selected = _inspect(normalized.data_root)
            if not selected.is_empty:
                raise MigrationError(
                    "migration requires an empty Native V2 runtime before publication"
                )
            imported = execute_v1_import(
                normalized.data_root,
                normalized.data_root,
                normalized.source_dir,
                expected_hashes=before_hashes,
                profile=profile,
                source_hashes=source_hashes,
                writer_lease=writer_lease,
                provenance=provenance,
            )
            selected = _inspect(normalized.data_root)
            _require_healthy_native(selected)
            if selected.active_snapshot_id != imported.candidate.snapshot_id:
                raise MigrationError("runtime selected a different snapshot after migration")
            _require_legacy_unchanged(normalized.data_root, before_hashes)
            if (
                _source_pdf_hashes(legacy_paths["reports.db"], normalized.source_dir)
                != source_hashes
            ):
                raise MigrationError("source PDFs changed before V1 cleanup")
            if imported.cleanup_marker_relative_path != V1_CLEANUP_MARKER:
                raise MigrationError("V1 cleanup marker was written to an unexpected path")
            _require_cleanup_authorized(
                normalized.data_root,
                selected,
                expected_v1_hashes=before_hashes,
                expected_source_hashes=source_hashes,
            )
            removed = _retire_v1_artifacts(normalized.data_root)
    return _outcome(
        "migrated",
        selected,
        vector_count=imported.vector_count,
        max_vector_absolute_error=imported.max_vector_absolute_error,
        removed_v1_artifacts=removed,
    )


def _validated_settings(settings: UserMigrationSettings) -> UserMigrationSettings:
    if not isinstance(settings, UserMigrationSettings):
        raise MigrationError("migration settings are invalid")
    data_root = _plain_directory(settings.data_root, "DATA_ROOT")
    source_dir = _plain_directory(settings.source_dir, "source PDF directory")
    if source_dir != data_root / "downloaded":
        raise MigrationError("V1 migration requires the standard DATA_ROOT/downloaded layout")
    if not isinstance(settings.model, str) or not settings.model.strip():
        raise MigrationError("V1 embedding model name is missing")
    if not isinstance(settings.extractor, str) or not settings.extractor.strip():
        raise MigrationError("V1 extractor name is missing")
    for name, value in (
        ("parent chunk size", settings.parent_chunk_size),
        ("child chunk size", settings.child_chunk_size),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MigrationError(f"{name} must be a positive integer")
    if settings.child_chunk_size > settings.parent_chunk_size:
        raise MigrationError("child chunks cannot be larger than parent chunks")
    if settings.use_parent_child is not True:
        raise MigrationError("V1 migration requires USE_PARENT_CHILD=true")
    fallback_extractor = str(settings.fallback_extractor or "").strip() or None
    if fallback_extractor == settings.extractor.strip():
        fallback_extractor = None
    try:
        format_legacy_import_extraction_profile(
            settings.extractor,
            allow_fallback=fallback_extractor is not None,
            fallback_engine=fallback_extractor,
            allow_custom=True,
        )
    except NativeBuildError as exc:
        raise MigrationError("future Native V2 extraction policy is invalid") from exc
    return UserMigrationSettings(
        data_root=data_root,
        source_dir=source_dir,
        model=settings.model.strip(),
        extractor=settings.extractor.strip(),
        parent_chunk_size=settings.parent_chunk_size,
        child_chunk_size=settings.child_chunk_size,
        use_parent_child=True,
        fallback_extractor=fallback_extractor,
    )


def _validated_v1_artifacts(data_root: Path) -> dict[str, Path]:
    sidecars = [name for name in V1_OPTIONAL_SIDECARS if (data_root / name).exists()]
    if sidecars:
        raise MigrationError(
            "V1 reports database still has journal files; close every old app process "
            "before migration"
        )
    vector_root = _plain_directory(data_root / "vector_db", "V1 vector directory")
    allowed = {"index.faiss", "index.pkl"}
    unexpected = sorted(path.name for path in vector_root.iterdir() if path.name not in allowed)
    if unexpected:
        raise MigrationError(
            f"V1 vector directory contains an unexpected entry: {unexpected[0]}"
        )
    return {
        "reports.db": _plain_file(data_root / "reports.db", "V1 reports database"),
        "vector_db/index.faiss": _plain_file(
            vector_root / "index.faiss", "V1 FAISS index"
        ),
        "vector_db/index.pkl": _plain_file(
            vector_root / "index.pkl", "V1 LangChain docstore"
        ),
    }


def _embedding_profile(
    settings: UserMigrationSettings,
    *,
    dimension: int,
    metric: str,
) -> EmbeddingProfile:
    return EmbeddingProfile(
        model=settings.model,
        dimension=dimension,
        metric=metric,
        normalization="none",
        prefix_template=PREFIX_TEMPLATE,
        extractor=format_legacy_import_extraction_profile(
            settings.extractor,
            allow_fallback=settings.fallback_extractor is not None,
            fallback_engine=settings.fallback_extractor,
            allow_custom=True,
        ),
        parent_policy={
            "algorithm": "langchain-recursive-v1",
            "chunk_overlap": int(settings.parent_chunk_size * 0.1),
            "chunk_size": settings.parent_chunk_size,
            "headers": ["#", "##", "###"],
            "separators": ["\n\n", "\n", ". ", " ", ""],
        },
        child_policy={
            "algorithm": "langchain-recursive-v1",
            "chunk_overlap": int(settings.child_chunk_size * 0.1),
            "chunk_size": settings.child_chunk_size,
            "separators": ["\n\n", "\n", ". ", " ", ""],
            "span_source": "splitter-start-index",
        },
    )


def _source_pdf_hashes(database: Path, source_dir: Path) -> dict[str, str]:
    uri = f"file:{database.resolve(strict=True).as_posix()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT file_name FROM reports ORDER BY file_name"
                )
            ]
    except sqlite3.Error as exc:
        raise MigrationError(f"V1 report list cannot be read: {exc}") from exc
    if not names or len(names) != len(set(names)):
        raise MigrationError("V1 report list is empty or contains duplicates")
    hashes: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or Path(name).name != name or not name:
            raise MigrationError(f"V1 contains an unsafe PDF name: {name!r}")
        path = _plain_file(source_dir / name, f"source PDF {name}")
        if path.parent != source_dir:
            raise MigrationError(f"source PDF escapes DATA_ROOT/downloaded: {name}")
        hashes[name] = _sha256_file(path)
    observed = {
        path.name
        for path in source_dir.iterdir()
        if path.name.lower().endswith(".pdf")
    }
    if observed != set(names):
        raise MigrationError("source PDF directory and V1 report catalog differ")
    return hashes


def _require_legacy_unchanged(data_root: Path, expected: dict[str, str]) -> None:
    current = _validated_v1_artifacts(data_root)
    observed = {relative: _sha256_file(path) for relative, path in current.items()}
    if observed != expected:
        raise MigrationError("V1 artifacts changed while migration was running")


def _retire_v1_artifacts(data_root: Path) -> tuple[str, ...]:
    """Remove only the exact legacy files after Native V2 is healthy."""

    root = _plain_directory(data_root, "DATA_ROOT")
    removed: list[str] = []
    for relative in (*V1_ARTIFACTS, *V1_OPTIONAL_SIDECARS):
        path = root.joinpath(*relative.split("/"))
        if not _path_entry_exists(path):
            continue
        target = _plain_file(path, f"retired V1 artifact {relative}")
        try:
            target.unlink()
        except OSError as exc:
            raise MigrationError(
                "Native V2 is active, but V1 cleanup is incomplete; rerun "
                f"MIGRATE_V2.bat: {relative}: {exc}"
            ) from exc
        removed.append(relative)
    vector_root = root / "vector_db"
    if _path_entry_exists(vector_root):
        vector_root = _plain_directory(vector_root, "retired V1 vector directory")
        try:
            vector_root.rmdir()
        except OSError as exc:
            raise MigrationError(
                "Native V2 is active, but the V1 vector directory is not empty"
            ) from exc
    _remove_cleanup_marker(root)
    return tuple(relative for relative in V1_ARTIFACTS if relative in removed) + tuple(
        relative for relative in V1_OPTIONAL_SIDECARS if relative in removed
    )


def _has_v1_artifacts(data_root: Path) -> bool:
    paths = [
        data_root.joinpath(*relative.split("/"))
        for relative in (*V1_ARTIFACTS, *V1_OPTIONAL_SIDECARS)
    ]
    paths.append(data_root / "vector_db")
    return any(_path_entry_exists(path) for path in paths)


def _require_cleanup_authorized(
    data_root: Path,
    selection: RuntimeSelection,
    *,
    expected_v1_hashes: dict[str, str] | None = None,
    expected_source_hashes: dict[str, str] | None = None,
) -> None:
    marker_path = data_root.joinpath(*V1_CLEANUP_MARKER.split("/"))
    marker_path = _plain_file(marker_path, "V1 cleanup marker")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError("V1 cleanup marker cannot be read") from exc
    required = {
        "schema_version",
        "kind",
        "snapshot_id",
        "build_id",
        "publication_id",
        "assessment_digest",
        "reconstruction_digest",
        "vector_payload_sha256",
        "vector_count",
        "v1_artifact_sha256",
        "source_pdf_sha256",
    }
    if not isinstance(marker, dict) or set(marker) != required:
        raise MigrationError("V1 cleanup marker has an unsupported shape")
    if marker["schema_version"] != 1 or marker["kind"] != "native-v2-v1-cleanup":
        raise MigrationError("V1 cleanup marker identity is invalid")
    if (
        marker["snapshot_id"] != selection.active_snapshot_id
        or marker["build_id"] != selection.active_build_id
    ):
        raise MigrationError(
            "active Native V2 was not produced by the pending V1 migration; "
            "V1 files were not deleted"
        )
    marker_hashes = marker["v1_artifact_sha256"]
    if not isinstance(marker_hashes, dict) or set(marker_hashes) != set(V1_ARTIFACTS):
        raise MigrationError("V1 cleanup marker does not cover the legacy artifacts")
    if expected_v1_hashes is not None and marker_hashes != expected_v1_hashes:
        raise MigrationError("V1 cleanup marker hashes differ from this migration")
    marker_sources = marker["source_pdf_sha256"]
    if not isinstance(marker_sources, dict) or not marker_sources:
        raise MigrationError("V1 cleanup marker has no source PDF inventory")
    if expected_source_hashes is not None and marker_sources != expected_source_hashes:
        raise MigrationError("V1 cleanup marker source inventory differs from this migration")
    for relative, expected_hash in marker_hashes.items():
        path = data_root.joinpath(*relative.split("/"))
        if not path.exists():
            continue
        observed = _sha256_file(_plain_file(path, f"pending V1 artifact {relative}"))
        if observed != expected_hash:
            raise MigrationError(f"pending V1 artifact changed after migration: {relative}")


def _remove_completed_cleanup_marker(
    data_root: Path,
    selection: RuntimeSelection,
) -> None:
    marker = data_root.joinpath(*V1_CLEANUP_MARKER.split("/"))
    if not marker.exists():
        return
    _require_cleanup_authorized(data_root, selection)
    _remove_cleanup_marker(data_root)


def _remove_cleanup_marker(data_root: Path) -> None:
    marker = data_root.joinpath(*V1_CLEANUP_MARKER.split("/"))
    if _path_entry_exists(marker):
        _plain_file(marker, "V1 cleanup marker").unlink()
    directory = marker.parent
    if _path_entry_exists(directory):
        directory = _plain_directory(directory, "V1 migration marker directory")
        if not any(directory.iterdir()):
            directory.rmdir()


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise MigrationError(f"path cannot be inspected safely: {path}: {exc}") from exc
    return True


def _inspect(data_root: Path) -> RuntimeSelection:
    try:
        return inspect_runtime(data_root, validate_snapshot=True)
    except Exception as exc:
        raise MigrationError(
            f"Native V2 runtime cannot be inspected: {type(exc).__name__}: {exc}"
        ) from exc


def _require_healthy_native(selection: RuntimeSelection) -> None:
    if (
        not selection.is_native
        or selection.is_empty
        or not selection.active_snapshot_id
        or selection.write_epoch <= 0
        or selection.degraded
        or not selection.write_enabled
    ):
        raise MigrationError("Native V2 is not healthy and writable")


def _active_vector_count(selection: RuntimeSelection) -> int:
    if not selection.active_snapshot_id:
        return 0
    uri = f"file:{selection.paths.catalog.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT ntotal FROM vector_snapshots WHERE snapshot_id = ?",
            (selection.active_snapshot_id,),
        ).fetchone()
    return 0 if row is None else int(row[0])


def _outcome(
    status: str,
    selection: RuntimeSelection,
    *,
    vector_count: int,
    max_vector_absolute_error: float,
    removed_v1_artifacts: tuple[str, ...],
) -> MigrationOutcome:
    return MigrationOutcome(
        status=status,
        snapshot_id=selection.active_snapshot_id or "",
        publication_generation=selection.publication_generation,
        write_epoch=selection.write_epoch,
        write_enabled=selection.write_enabled,
        vector_count=vector_count,
        max_vector_absolute_error=max_vector_absolute_error,
        removed_v1_artifacts=removed_v1_artifacts,
    )


def _plain_directory(path: str | Path, label: str) -> Path:
    lexical = Path(path).expanduser().absolute()
    if not lexical.is_dir():
        raise MigrationError(f"{label} is missing: {lexical}")
    _require_plain_local_path(lexical, label)
    return lexical.resolve(strict=True)


def _plain_file(path: str | Path, label: str) -> Path:
    lexical = Path(path).expanduser().absolute()
    if not lexical.is_file():
        raise MigrationError(f"{label} is missing: {lexical}")
    _require_plain_local_path(lexical, label)
    return lexical.resolve(strict=True)


def _require_plain_local_path(path: Path, label: str) -> None:
    absolute = path.absolute()
    if str(absolute).startswith("\\\\"):
        raise MigrationError(f"{label} must be on a local drive")
    for candidate in (absolute, *absolute.parents):
        if not candidate.exists():
            continue
        if candidate.is_symlink() or _is_reparse_point(candidate):
            raise MigrationError(f"{label} cannot traverse a symlink or reparse point")


def _is_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _settings_from_cli(data_root_override: Path | None) -> UserMigrationSettings:
    from src.configs import config

    data_root = Path(data_root_override or config.DATA_ROOT).expanduser().absolute()
    main_extractor = str(config.PDF_EXTRACTION_ENGINE or "").strip()
    override_extractor = str(config.UNEMBEDDED_PDF_EXTRACTION_ENGINE or "").strip()
    extractor = override_extractor or main_extractor
    configured_fallback = str(config.PDF_EXTRACTION_FALLBACK_ENGINE or "").strip()
    fallback_extractor = configured_fallback if extractor == main_extractor else None
    if fallback_extractor == extractor:
        fallback_extractor = None
    return UserMigrationSettings(
        data_root=data_root,
        source_dir=data_root / "downloaded",
        model=str(config.EMBEDDING_MODEL),
        extractor=extractor,
        parent_chunk_size=int(config.PARENT_CHUNK_SIZE),
        child_chunk_size=int(config.CHILD_CHUNK_SIZE),
        use_parent_child=bool(config.USE_PARENT_CHILD),
        fallback_extractor=fallback_extractor,
    )


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reuse a standard V1 corpus and activate it as Native V2"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        help="V1 data directory; defaults to configured DATA_ROOT",
    )
    args = parser.parse_args(argv)
    _configure_console()
    print("=" * 68)
    print("Finance LLM V1 → Native V2 마이그레이션")
    print("기존 청크와 FAISS 벡터를 재사용하며 전체 재임베딩은 하지 않습니다.")
    print("성공 후 reports.db와 vector_db는 삭제하고 downloaded PDF는 유지합니다.")
    print("=" * 68)
    try:
        outcome = migrate_v1_to_v2(_settings_from_cli(args.data_root))
    except KeyboardInterrupt:
        print("\n사용자 요청으로 중단했습니다.")
        return 130
    except Exception as exc:
        print(f"\n[실패] {exc}")
        print("Native V2 활성화 전 실패했다면 V1 원본은 그대로 유지됩니다.")
        return 1

    if outcome.status == "already_migrated":
        print("\n[완료] 이미 쓰기 가능한 Native V2가 활성화되어 있습니다.")
    else:
        print("\n[완료] 기존 벡터를 Native V2로 전환했습니다.")
    if outcome.removed_v1_artifacts:
        print("삭제된 V1 아티팩트: " + ", ".join(outcome.removed_v1_artifacts))
    print(f"벡터 수: {outcome.vector_count}")
    print("downloaded PDF는 이후 업데이트와 재구축을 위해 유지됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MigrationError",
    "MigrationOutcome",
    "UserMigrationSettings",
    "migrate_v1_to_v2",
]
