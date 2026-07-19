"""Runtime validation contract for a sealed epoch-zero compatibility bundle.

Bundle creation belongs to the V1-to-V2 migration package. Runtime selection
only validates already-sealed artifacts and must not import migration tooling.
"""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.retrieval.identity import canonical_json, sha256_text


BUNDLE_MANIFEST_VERSION = 1


class CompatibilityBundleError(ValueError):
    """Raised when sealed compatibility evidence is unsafe or incomplete."""


@dataclass(frozen=True)
class EvidenceArtifact:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class CompatibilityBundleManifest:
    bundle_id: str
    artifacts: tuple[EvidenceArtifact, ...]
    state: str = "sealed"
    selectable_at_epoch_zero: bool = True
    cleanup_pending: bool = False
    schema_version: int = BUNDLE_MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def manifest_sha256(self) -> str:
        return sha256_text(self.canonical_json)


def validate_compatibility_bundle(
    data_root: str | Path,
    bundle_id: str,
) -> CompatibilityBundleManifest:
    root = _plain_directory(data_root)
    if not isinstance(bundle_id, str) or len(bundle_id) != 64:
        raise CompatibilityBundleError("compatibility bundle ID must be a SHA-256 digest")
    bundle = root / "retrieval" / "compat" / "v1" / bundle_id
    if not bundle.is_dir() or bundle.is_symlink():
        raise CompatibilityBundleError("compatibility bundle directory is missing or linked")
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise CompatibilityBundleError("compatibility bundle manifest is missing or linked")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = CompatibilityBundleManifest(
            bundle_id=payload["bundle_id"],
            artifacts=tuple(EvidenceArtifact(**item) for item in payload["artifacts"]),
            state=payload["state"],
            selectable_at_epoch_zero=payload["selectable_at_epoch_zero"],
            cleanup_pending=payload["cleanup_pending"],
            schema_version=payload["schema_version"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CompatibilityBundleError(
            f"compatibility bundle manifest is invalid: {exc}"
        ) from exc
    if manifest.schema_version != BUNDLE_MANIFEST_VERSION:
        raise CompatibilityBundleError("unsupported compatibility bundle manifest version")
    if manifest.bundle_id != bundle_id:
        raise CompatibilityBundleError("compatibility bundle ID does not match its directory")
    if manifest.state != "sealed" or not manifest.selectable_at_epoch_zero:
        raise CompatibilityBundleError("compatibility bundle is not an epoch-zero sealed bundle")
    if manifest.cleanup_pending:
        raise CompatibilityBundleError(
            "newly sealed compatibility bundle cannot be cleanup_pending"
        )
    expected_names = {"reports.db", "index.faiss", "index.pkl"}
    if {item.relative_path for item in manifest.artifacts} != expected_names:
        raise CompatibilityBundleError("compatibility bundle artifact set is incomplete")
    for expected in manifest.artifacts:
        path = bundle / expected.relative_path
        if path.is_symlink() or not path.is_file():
            raise CompatibilityBundleError(
                f"compatibility artifact is missing or linked: {expected.relative_path}"
            )
        if _artifact(path, expected.relative_path) != expected:
            raise CompatibilityBundleError(
                f"compatibility artifact hash mismatch: {expected.relative_path}"
            )
        if _has_write_bits(path):
            raise CompatibilityBundleError(
                f"compatibility artifact is not read-only: {expected.relative_path}"
            )
    if _has_write_bits(manifest_path):
        raise CompatibilityBundleError("compatibility manifest is not read-only")
    return manifest


def _plain_directory(value: str | Path) -> Path:
    path = Path(value).absolute()
    if not path.is_dir() or path.is_symlink():
        raise CompatibilityBundleError("data root must be a real directory")
    return path.resolve(strict=True)


def _artifact(path: Path, relative_path: str) -> EvidenceArtifact:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return EvidenceArtifact(relative_path, path.stat().st_size, digest.hexdigest())


def _has_write_bits(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


__all__ = [
    "BUNDLE_MANIFEST_VERSION",
    "CompatibilityBundleError",
    "CompatibilityBundleManifest",
    "EvidenceArtifact",
    "validate_compatibility_bundle",
]
