"""Copied-install and sealed epoch-zero V1 compatibility evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.migrations.v2.assess import DEFAULT_ARTIFACT_PATHS
from src.retrieval.compatibility_bundle import (
    BUNDLE_MANIFEST_VERSION,
    CompatibilityBundleError,
    CompatibilityBundleManifest,
    EvidenceArtifact,
    validate_compatibility_bundle,
)
from src.retrieval.identity import canonical_json, sha256_text


COPY_MANIFEST_VERSION = 1
EvidenceError = CompatibilityBundleError


@dataclass(frozen=True)
class CopiedInstallEvidence:
    source_artifacts: tuple[EvidenceArtifact, ...]
    copied_artifacts: tuple[EvidenceArtifact, ...]
    source_report_count: int
    source_parent_count: int
    created_at_utc: str
    schema_version: int = COPY_MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_copied_v1_install(
    source_root: str | Path,
    destination_root: str | Path,
) -> CopiedInstallEvidence:
    """Create an off-path V1 fixture using SQLite online backup and byte copies.

    Source hashes are taken both before and after the operation.  Any drift
    fails the copy, so a concurrently mutating FAISS/docstore pair can never be
    presented as a coherent migration input.
    """

    source = _plain_directory(source_root, must_exist=True)
    destination = Path(destination_root).absolute()
    if destination.exists():
        raise FileExistsError(f"copied install destination already exists: {destination.name}")
    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    staging = destination_parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    try:
        source_paths = _source_artifact_paths(source)
        source_before = tuple(
            _artifact(path, relative)
            for relative, path in source_paths.items()
        )
        report_count, parent_count = _online_backup(
            source_paths["reports.db"], staging / "reports.db"
        )
        vector_destination = staging / "vector_db"
        vector_destination.mkdir()
        for relative in ("vector_db/index.faiss", "vector_db/index.pkl"):
            target = staging / relative
            shutil.copyfile(source_paths[relative], target)
            _fsync_file(target)

        copied = tuple(
            _artifact(staging / relative, relative)
            for relative in DEFAULT_ARTIFACT_PATHS
        )
        source_after = tuple(
            _artifact(path, relative)
            for relative, path in source_paths.items()
        )
        if source_before != source_after:
            raise EvidenceError("source V1 artifacts changed during copied-install capture")
        # FAISS and pickle are required byte copies.  SQLite online-backup bytes
        # may differ while preserving the same canonical database content.
        for relative in ("vector_db/index.faiss", "vector_db/index.pkl"):
            original = next(item for item in source_before if item.relative_path == relative)
            replica = next(item for item in copied if item.relative_path == relative)
            if (original.size_bytes, original.sha256) != (
                replica.size_bytes,
                replica.sha256,
            ):
                raise EvidenceError(f"copied artifact differs from V1 source: {relative}")

        evidence = CopiedInstallEvidence(
            source_artifacts=source_before,
            copied_artifacts=copied,
            source_report_count=report_count,
            source_parent_count=parent_count,
            created_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        _write_json(staging / "copy-manifest.json", evidence.to_dict())
        _fsync_directory(staging)
        os.rename(staging, destination)
        _fsync_directory(destination_parent)
        return evidence
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def seal_compatibility_bundle(
    copied_install_root: str | Path,
    data_root: str | Path,
) -> CompatibilityBundleManifest:
    """Seal the exact copied V1 artifacts under retrieval/compat/v1/<hash>."""

    source = _plain_directory(copied_install_root, must_exist=True)
    root = _plain_directory(data_root, must_exist=False)
    source_paths = _source_artifact_paths(source)
    source_artifacts = tuple(
        _artifact(source_paths[relative], relative)
        for relative in DEFAULT_ARTIFACT_PATHS
    )
    bundle_id = sha256_text(
        canonical_json(
            {
                "schema_version": BUNDLE_MANIFEST_VERSION,
                "artifacts": [asdict(item) for item in source_artifacts],
            }
        )
    )
    bundle_parent = root / "retrieval" / "compat" / "v1"
    bundle_parent.mkdir(parents=True, exist_ok=True)
    target = bundle_parent / bundle_id
    if target.exists():
        return validate_compatibility_bundle(root, bundle_id)

    staging = bundle_parent / f".{bundle_id}.{uuid.uuid4().hex}.tmp"
    staging.mkdir()
    try:
        bundle_artifacts: list[EvidenceArtifact] = []
        source_to_bundle = {
            "reports.db": "reports.db",
            "vector_db/index.faiss": "index.faiss",
            "vector_db/index.pkl": "index.pkl",
        }
        for source_relative, bundle_relative in source_to_bundle.items():
            destination = staging / bundle_relative
            shutil.copyfile(source_paths[source_relative], destination)
            _fsync_file(destination)
            copied = _artifact(destination, bundle_relative)
            original = next(
                item for item in source_artifacts if item.relative_path == source_relative
            )
            if (copied.size_bytes, copied.sha256) != (
                original.size_bytes,
                original.sha256,
            ):
                raise EvidenceError(f"sealed bundle copy differs: {bundle_relative}")
            bundle_artifacts.append(copied)

        manifest = CompatibilityBundleManifest(
            bundle_id=bundle_id,
            artifacts=tuple(bundle_artifacts),
        )
        _write_json(staging / "manifest.json", manifest.to_dict())
        for path in staging.iterdir():
            _make_read_only(path)
        _fsync_directory(staging)
        os.rename(staging, target)
        _fsync_directory(bundle_parent)
        return validate_compatibility_bundle(root, bundle_id)
    except BaseException:
        _make_tree_writable(staging)
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _source_artifact_paths(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    root_uid = getattr(root.stat(), "st_uid", None)
    for relative in DEFAULT_ARTIFACT_PATHS:
        path = root / relative
        current = root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise EvidenceError(f"V1 source path contains a symlink: {relative}")
        if not path.is_file():
            raise EvidenceError(f"V1 source artifact is missing: {relative}")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise EvidenceError("V1 source artifact resolves outside its root") from exc
        path_uid = getattr(resolved.stat(), "st_uid", None)
        if root_uid is not None and path_uid is not None and root_uid != path_uid:
            raise EvidenceError(f"V1 source artifact owner differs: {relative}")
        result[relative] = resolved
    return result


def _plain_directory(value: str | Path, *, must_exist: bool) -> Path:
    path = Path(value).absolute()
    if must_exist:
        if not path.is_dir() or path.is_symlink():
            raise EvidenceError("evidence root must be a real directory")
        return path.resolve(strict=True)
    if path.exists() and (not path.is_dir() or path.is_symlink()):
        raise EvidenceError("data root must be a real directory")
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve(strict=True)


def _online_backup(source: Path, destination: Path) -> tuple[int, int]:
    source_uri = f"file:{source.as_posix()}?mode=ro"
    try:
        source_connection = sqlite3.connect(source_uri, uri=True)
        try:
            source_connection.execute("PRAGMA query_only = ON")
            if source_connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise EvidenceError("source SQLite quick_check failed")
            if source_connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise EvidenceError("source SQLite integrity_check failed")
            if source_connection.execute("PRAGMA foreign_key_check").fetchall():
                raise EvidenceError("source SQLite foreign-key check failed")
            report_count = source_connection.execute(
                "SELECT COUNT(*) FROM reports"
            ).fetchone()[0]
            parent_count = source_connection.execute(
                "SELECT COUNT(*) FROM parent_chunks"
            ).fetchone()[0]
            target_connection = sqlite3.connect(destination)
            try:
                source_connection.backup(target_connection)
                target_connection.commit()
            finally:
                target_connection.close()
        finally:
            source_connection.close()
        _fsync_file(destination)
        check = sqlite3.connect(f"file:{destination.as_posix()}?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise EvidenceError("copied SQLite integrity_check failed")
            if check.execute("PRAGMA foreign_key_check").fetchall():
                raise EvidenceError("copied SQLite foreign-key check failed")
            if check.execute("SELECT COUNT(*) FROM reports").fetchone()[0] != report_count:
                raise EvidenceError("copied SQLite report count changed")
            if check.execute("SELECT COUNT(*) FROM parent_chunks").fetchone()[0] != parent_count:
                raise EvidenceError("copied SQLite parent count changed")
        finally:
            check.close()
        return int(report_count), int(parent_count)
    except sqlite3.Error as exc:
        raise EvidenceError(f"SQLite online backup failed: {exc}") from exc


def _artifact(path: Path, relative_path: str) -> EvidenceArtifact:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return EvidenceArtifact(relative_path, path.stat().st_size, digest.hexdigest())


def _write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_read_only(path: Path) -> None:
    path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _has_write_bits(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _make_tree_writable(path: Path) -> None:
    if not path.exists():
        return
    for item in path.rglob("*"):
        try:
            item.chmod(stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass


__all__ = [
    "CompatibilityBundleManifest",
    "CopiedInstallEvidence",
    "EvidenceArtifact",
    "EvidenceError",
    "create_copied_v1_install",
    "seal_compatibility_bundle",
    "validate_compatibility_bundle",
]
