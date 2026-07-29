"""Safe environment fingerprints for feedback candidate reproduction."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.configs import settings
from src.core.app_version import get_app_version


SCHEMA_VERSION = 1
_HEX64 = set("0123456789abcdef")
_REQUIRED_IDENTITY_FIELDS = (
    "app_version",
    "code_revision",
    "code_fingerprint",
    "model_fingerprint",
    "prompt_fingerprint",
    "tool_fingerprint",
    "data_revision",
    "index_revision",
    "config_fingerprint",
    "feature_flags_fingerprint",
)
_MANIFEST_KEYS = {
    "schema_version",
    *_REQUIRED_IDENTITY_FIELDS,
    "complete",
    "missing_fields",
    "manifest_hash",
}
_SECRET_NAME_PARTS = (
    "API_KEY",
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "TOKEN",
    "CREDENTIAL",
)


class ReproductionManifestError(ValueError):
    """Raised when a reproduction manifest is malformed or incomplete."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReproductionManifestError(
            "reproduction manifest contains unsupported values"
        ) from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX64 for character in value)
    )


def _hash_mapping(value: Mapping[str, Any]) -> str:
    return _sha256(_canonical_bytes(value))


def _identity_value_is_missing(field: str, value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    if value.strip().lower() in {"unknown", "unavailable", "none"}:
        return True
    return field.endswith("_fingerprint") and not _is_sha256(value)


def _hash_files(root: Path, paths: Sequence[Path]) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted({item.resolve() for item in paths}):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root.resolve()).as_posix()
        except ValueError:
            relative = path.name
        entries.append(
            {
                "path": relative,
                "sha256": _sha256(path.read_bytes()),
            }
        )
    return _hash_mapping({"files": entries})


def _git_revision(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    revision = completed.stdout.strip()
    return revision if revision else "unavailable"


def _safe_config_values() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in settings.CONFIG_SPECS:
        if any(part in name.upper() for part in _SECRET_NAME_PARTS):
            continue
        try:
            value = settings.get_config_value(name)
        except (TypeError, ValueError):
            value = "<invalid>"
        values[name] = value
    return values


def build_reproduction_manifest(
    *,
    app_version: str,
    code_revision: str,
    code_fingerprint: str,
    model_fingerprint: str,
    prompt_fingerprint: str,
    tool_fingerprint: str,
    data_revision: str | None,
    index_revision: str | None,
    config_fingerprint: str,
    feature_flags_fingerprint: str,
) -> dict[str, Any]:
    """Build one exact, hashed, allowlisted reproduction manifest."""

    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "app_version": str(app_version or "").strip(),
        "code_revision": str(code_revision or "").strip(),
        "code_fingerprint": code_fingerprint,
        "model_fingerprint": model_fingerprint,
        "prompt_fingerprint": prompt_fingerprint,
        "tool_fingerprint": tool_fingerprint,
        "data_revision": str(data_revision or "").strip(),
        "index_revision": str(index_revision or "").strip(),
        "config_fingerprint": config_fingerprint,
        "feature_flags_fingerprint": feature_flags_fingerprint,
    }
    missing = [
        field
        for field in _REQUIRED_IDENTITY_FIELDS
        if _identity_value_is_missing(field, identity.get(field))
    ]
    payload = {
        **identity,
        "complete": not missing,
        "missing_fields": missing,
    }
    payload["manifest_hash"] = _hash_mapping(payload)
    return canonicalize_reproduction_manifest(payload)


def build_runtime_reproduction_manifest(
    *,
    repo_root: str | Path | None = None,
    data_revision: str | None = None,
    index_revision: str | None = None,
    app_version: str | None = None,
) -> dict[str, Any]:
    """Fingerprint current code/model/prompt/tool/config without exporting values."""

    root = Path(repo_root or settings.BASE_DIR).resolve()
    source_files = list((root / "src").rglob("*.py"))
    prompt_files = [root / "src" / "configs" / "prompts.py"]
    tool_files = [
        *list((root / "src" / "nodes").glob("*.py")),
        *list((root / "src" / "graphs").glob("*.py")),
    ]
    safe_config = _safe_config_values()
    model_identity = {
        key: safe_config.get(key)
        for key in (
            "GENERATION_MODEL",
            "EMBEDDING_MODEL",
            "RERANK_PROVIDER",
            "RERANK_MODEL",
        )
    }
    feature_flags = {
        key: safe_config.get(key)
        for key in (
            "MONITORING_MODE",
            "USE_PARENT_CHILD",
            "USE_RERANKER",
        )
    }
    return build_reproduction_manifest(
        app_version=app_version or get_app_version(),
        code_revision=_git_revision(root),
        code_fingerprint=_hash_files(root, source_files),
        model_fingerprint=_hash_mapping(model_identity),
        prompt_fingerprint=_hash_files(root, prompt_files),
        tool_fingerprint=_hash_files(root, tool_files),
        data_revision=data_revision,
        index_revision=index_revision,
        config_fingerprint=_hash_mapping(safe_config),
        feature_flags_fingerprint=_hash_mapping(feature_flags),
    )


def canonicalize_reproduction_manifest(
    value: Any,
) -> dict[str, Any]:
    """Validate exact fields and recompute completeness without mutating input."""

    if not isinstance(value, Mapping):
        raise ReproductionManifestError(
            "reproduction_manifest must be an object"
        )
    unknown = set(value) - _MANIFEST_KEYS
    missing_keys = _MANIFEST_KEYS - set(value)
    if unknown or missing_keys:
        raise ReproductionManifestError(
            "reproduction_manifest fields do not match the schema"
        )
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ReproductionManifestError(
            "reproduction_manifest schema_version is invalid"
        )
    manifest = dict(value)
    for field in _REQUIRED_IDENTITY_FIELDS:
        item = manifest.get(field)
        if not isinstance(item, str):
            raise ReproductionManifestError(
                f"reproduction_manifest.{field} must be a string"
            )
        if field.endswith("_fingerprint") and item and not _is_sha256(item):
            raise ReproductionManifestError(
                f"reproduction_manifest.{field} is not sha256"
            )
    expected_missing = [
        field
        for field in _REQUIRED_IDENTITY_FIELDS
        if _identity_value_is_missing(field, manifest.get(field))
    ]
    if (
        manifest.get("missing_fields") != expected_missing
        or not isinstance(manifest.get("complete"), bool)
        or bool(manifest.get("complete")) != (not expected_missing)
        or not _is_sha256(manifest.get("manifest_hash"))
    ):
        raise ReproductionManifestError(
            "reproduction_manifest completeness metadata is invalid"
        )
    without_hash = dict(manifest)
    without_hash.pop("manifest_hash")
    if _hash_mapping(without_hash) != manifest["manifest_hash"]:
        raise ReproductionManifestError(
            "reproduction_manifest hash mismatch"
        )
    return manifest


def require_complete_reproduction_manifest(
    value: Any,
) -> dict[str, Any]:
    manifest = canonicalize_reproduction_manifest(value)
    if manifest["complete"] is not True:
        raise ReproductionManifestError(
            "reproduction_manifest is incomplete: "
            + ", ".join(manifest["missing_fields"])
        )
    return manifest
