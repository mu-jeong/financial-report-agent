"""Immutable runnable-release assets and process-isolated execution.

This module deliberately does not own workflow state or a registry.  It gives a
registry/orchestrator a small filesystem contract: validate a temporary bundle,
atomically publish immutable bytes, derive current availability, and execute the
published runner against one explicit fixed snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.core import artifact_io


FIRST_BASELINE_VERSION = "0.6.1"
FIRST_BASELINE_GIT_REVISION = "aac850769e97388884e49c0068ea97f691e06d9e"
RELEASE_SCHEMA_VERSION = 1
RUNNER_CONTRACT_VERSION = 1
SNAPSHOT_READER_CONTRACT_VERSION = 2

_BUNDLE_NAMES = {
    "release-manifest.json",
    "app",
    "runtime",
    "runtime-profile.json",
    "runner.json",
    "object-hashes.json",
}
_HASH_INPUT_FILES = {"runtime-profile.json", "runner.json"}
_HASH_INPUT_DIRS = {"app", "runtime"}
_VCS_METADATA_DIRECTORIES = {".git", ".hg", ".svn"}
_SENSITIVE_FILE_NAMES = {
    ".netrc",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
}
_SENSITIVE_FILE_SUFFIXES = {
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pfx",
}
_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)

# A historical runner must not inherit credentials or checkout-selection knobs
# from the operator process.  Only process-launch essentials are inherited;
# model access is an explicit, per-run hand-off and is never persisted in a
# release, fixture, snapshot, or Run artifact.
_INHERITED_PROCESS_ENVIRONMENT = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
}
_EXPLICIT_RUNNER_ENVIRONMENT = {
    "OPENROUTER_API_KEY",
    "RERANK_API_KEY",
}
_RUNTIME_PROFILE_ENVIRONMENT = {
    "CHILD_CHUNK_SIZE",
    "CHUNK_SIZE",
    "EMBEDDING_MODEL",
    "GENERATION_MODEL",
    "OPENROUTER_APP_TITLE",
    "OPENROUTER_APP_URL",
    "OPENROUTER_DATA_COLLECTION",
    "PARENT_CHUNK_SIZE",
    "RECENCY_WEIGHT",
    "RERANK_MODEL",
    "RERANK_PROVIDER",
    "RERANK_TIMEOUT",
    "SEARCH_CANDIDATE_MULTIPLIER",
    "SEARCH_TOP_K",
    "USE_PARENT_CHILD",
    "USE_RERANKER",
    "VECTOR_RETRIEVAL_CONCURRENCY",
}


class ReleaseAssetError(RuntimeError):
    """Raised when immutable release-asset safety cannot be proved."""


class ReleaseAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    LOCAL_MISSING = "LOCAL_MISSING"
    CORRUPT = "CORRUPT"
    INCOMPATIBLE = "INCOMPATIBLE"


def _validate_official_release_identity(
    *, app_version: str, git_revision: str
) -> None:
    if app_version == "0.6.0":
        raise ReleaseAssetError("v0.6.0 was local-only and must not be registered")
    if (
        app_version == FIRST_BASELINE_VERSION
        and git_revision != FIRST_BASELINE_GIT_REVISION
    ):
        raise ReleaseAssetError(
            "v0.6.1 must use the official remote baseline revision"
        )


@dataclass(frozen=True)
class ValidatedRelease:
    stage_path: Path
    release_manifest_id: str
    app_version: str
    git_revision: str
    build_digest: str
    runtime_bundle_digest: str
    runtime_profile_digest: str
    runner_contract_version: int
    snapshot_reader_contract_version: int


@dataclass(frozen=True)
class ReleaseDescriptor:
    release_manifest_id: str
    app_version: str
    git_revision: str
    build_digest: str
    runtime_bundle_digest: str
    runtime_profile_digest: str
    runner_contract_version: int
    snapshot_reader_contract_version: int
    path: Path
    # Durable REGISTERED state belongs to the caller's registry/control plane.
    state: str = "REGISTERED_READY"


@dataclass(frozen=True)
class ReleaseExecutionResult:
    returncode: int
    artifact_path: Path
    artifact_digest: str
    workspace_path: Path
    stdout: str
    stderr: str
    cleanup_warning: str | None


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_source_file(path: Path, *, root: Path) -> None:
    relative = path.relative_to(root)
    lowered_parts = tuple(part.casefold() for part in relative.parts)
    name = lowered_parts[-1]
    if any(part in _VCS_METADATA_DIRECTORIES for part in lowered_parts[:-1]):
        raise ReleaseAssetError(f"sensitive file is not allowed in a release: {relative}")
    if (
        (name.startswith(".env") and name != ".env.example")
        or name in _SENSITIVE_FILE_NAMES
        or path.suffix.casefold() in _SENSITIVE_FILE_SUFFIXES
    ):
        raise ReleaseAssetError(f"sensitive file is not allowed in a release: {relative}")
    with path.open("rb") as handle:
        prefix = handle.read(64 * 1024)
    if any(marker in prefix for marker in _PRIVATE_KEY_MARKERS):
        raise ReleaseAssetError(
            f"private key content is not allowed in a release: {relative}"
        )


def _regular_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise ReleaseAssetError(f"symbolic link is not allowed: {root}")
    if not root.is_dir():
        raise ReleaseAssetError(f"required directory is missing: {root.name}")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseAssetError(f"symbolic link is not allowed: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseAssetError(f"non-regular file is not allowed: {path}")
        _validate_release_source_file(path, root=root)
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def canonical_tree_entries(root: str | Path) -> list[dict[str, Any]]:
    """Return path-sorted, content-addressed entries for a regular-file tree."""

    directory = Path(root)
    return [
        {
            "path": path.relative_to(directory).as_posix(),
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in _regular_files(directory)
    ]


def canonical_tree_digest(root: str | Path) -> str:
    """Hash a directory by canonical relative paths, sizes, and file hashes."""

    return _sha256_bytes(_canonical_json_bytes({"files": canonical_tree_entries(root)}))


def _managed_root(path: str | Path) -> Path:
    requested = Path(path)
    if requested.is_symlink():
        raise ReleaseAssetError("managed root must not be a symbolic link")
    root = requested.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ReleaseAssetError("managed root must be a real directory")
    return root


def _contained_path(path: str | Path, *, managed_root: str | Path) -> Path:
    root = Path(managed_root).resolve()
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ReleaseAssetError("path escapes the managed root") from exc
    if not relative.parts:
        raise ReleaseAssetError("the managed root itself cannot be used as an object path")
    cursor = root
    for component in relative.parts:
        cursor /= component
        if cursor.is_symlink():
            raise ReleaseAssetError("path inside the managed root contains a symbolic link")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseAssetError("path escapes the managed root") from exc
    return candidate


def _copy_tree_create_only(source: Path, target: Path) -> None:
    _regular_files(source)
    if target.exists():
        raise ReleaseAssetError(f"immutable target already exists: {target}")
    shutil.copytree(source, target, symlinks=False)


def _write_canonical_json(path: Path, payload: Mapping[str, Any]) -> None:
    artifact_io.atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
    )


def _write_canonical_json_create_only(
    path: Path, payload: Mapping[str, Any]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(_canonical_json_bytes(payload) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _copy_file_create_only(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def _prepare_fixed_snapshot_compatibility(
    snapshot: Path, workspace: Path
) -> None:
    """Preseed the durable floor expected by immutable v0.6.1 readers."""

    manifest_path = snapshot / "manifest.json"
    if manifest_path.is_symlink():
        raise ReleaseAssetError("FixedSnapshot manifest must be a regular file")
    if not manifest_path.exists():
        return
    if not manifest_path.is_file():
        raise ReleaseAssetError("FixedSnapshot manifest must be a regular file")
    manifest = _read_json_mapping(manifest_path)
    marker_keys = {"files", "projected_snapshot_id", "projected_build_id"}
    if not marker_keys.intersection(manifest):
        return
    if not marker_keys.issubset(manifest):
        raise ReleaseAssetError("FixedSnapshot manifest markers are incomplete")

    catalog_path = snapshot / "projected_catalog.sqlite3"
    if catalog_path.is_symlink() or not catalog_path.is_file():
        raise ReleaseAssetError("FixedSnapshot catalog must be a regular file")
    files = manifest.get("files")
    catalog_identity = (
        files.get("projected_catalog.sqlite3")
        if isinstance(files, Mapping)
        else None
    )
    actual_identity = {
        "sha256": _sha256_file(catalog_path),
        "size_bytes": catalog_path.stat().st_size,
    }
    if catalog_identity != actual_identity:
        raise ReleaseAssetError("FixedSnapshot catalog identity does not match manifest")

    projected_snapshot_id = manifest.get("projected_snapshot_id")
    projected_build_id = manifest.get("projected_build_id")
    if (
        not isinstance(projected_snapshot_id, str)
        or not projected_snapshot_id
        or not isinstance(projected_build_id, str)
        or not projected_build_id
    ):
        raise ReleaseAssetError("FixedSnapshot projected identity is invalid")
    try:
        connection = sqlite3.connect(
            f"{catalog_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True
        )
        try:
            runtime_rows = connection.execute(
                "SELECT active_snapshot_id, active_build_id, "
                "publication_generation, write_epoch "
                "FROM retrieval_runtime WHERE runtime_id=1"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise ReleaseAssetError("FixedSnapshot catalog runtime is invalid") from exc
    if runtime_rows != [(projected_snapshot_id, projected_build_id, 1, 0)]:
        raise ReleaseAssetError("FixedSnapshot catalog runtime does not match manifest")

    checkpoint_relative = (
        "retrieval/v2/backups/"
        "catalog-current-g1-fixed-snapshot-projection.sqlite3"
    )
    data_root = workspace / "isolated-data"
    checkpoint_path = data_root.joinpath(*checkpoint_relative.split("/"))
    _copy_file_create_only(catalog_path, checkpoint_path)
    if {
        "sha256": _sha256_file(checkpoint_path),
        "size_bytes": checkpoint_path.stat().st_size,
    } != actual_identity:
        raise ReleaseAssetError("FixedSnapshot checkpoint copy changed catalog bytes")
    _write_canonical_json_create_only(
        data_root
        / "retrieval"
        / "v2"
        / "evidence"
        / "fixed-snapshot-projection"
        / "committed-floor.json",
        {
            "schema_version": 2,
            "publication_id": "fixed-snapshot-projection",
            "publication_generation": 1,
            "write_epoch": 0,
            "active_snapshot_id": projected_snapshot_id,
            "checkpoint_relative_path": checkpoint_relative,
            "checkpoint_sha256": actual_identity["sha256"],
        },
    )


def _object_hash_payload(bundle: Path) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for directory_name in sorted(_HASH_INPUT_DIRS):
        directory = bundle / directory_name
        for entry in canonical_tree_entries(directory):
            objects.append(
                {
                    "path": f"{directory_name}/{entry['path']}",
                    "sha256": entry["sha256"],
                    "size_bytes": entry["size_bytes"],
                }
            )
    for file_name in sorted(_HASH_INPUT_FILES):
        path = bundle / file_name
        if path.is_symlink() or not path.is_file():
            raise ReleaseAssetError(f"required regular file is missing: {file_name}")
        objects.append(
            {
                "path": file_name,
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    objects.sort(key=lambda item: str(item["path"]))
    return {"schema_version": 1, "objects": objects}


def _release_identity_body(
    *,
    app_version: str,
    git_revision: str,
    build_digest: str,
    runtime_bundle_digest: str,
    runtime_profile_digest: str,
    runner_contract_version: int,
    snapshot_reader_contract_version: int,
) -> dict[str, Any]:
    return {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "app_version": app_version,
        "git_revision": git_revision,
        "build_digest": build_digest,
        "runtime_bundle_digest": runtime_bundle_digest,
        "runtime_profile_digest": runtime_profile_digest,
        "runner_contract_version": runner_contract_version,
        "snapshot_reader_contract_version": snapshot_reader_contract_version,
    }


def _read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = artifact_io.strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError(f"invalid JSON object: {path.name}") from exc
    if not isinstance(value, dict):
        raise ReleaseAssetError(f"JSON root must be an object: {path.name}")
    return value


def _validate_runtime_profile(profile: Mapping[str, Any]) -> None:
    environment = profile.get("environment", {})
    if not isinstance(environment, Mapping):
        raise ReleaseAssetError(
            "runtime profile environment must be an object"
        )
    for key, value in environment.items():
        normalized_key = str(key).upper()
        if normalized_key not in _RUNTIME_PROFILE_ENVIRONMENT:
            raise ReleaseAssetError(
                f"runtime profile environment key is not allowed: {key}"
            )
        if (
            value is None
            or isinstance(value, (dict, list))
            or "\x00" in str(value)
        ):
            raise ReleaseAssetError(
                f"runtime profile environment value is invalid: {key}"
            )


def prepare_release_stage(
    managed_root: str | Path,
    *,
    app_source: str | Path,
    runtime_source: str | Path,
    runtime_profile: Mapping[str, Any],
    runner: Mapping[str, Any],
    app_version: str,
    git_revision: str,
    package_version: str | None = None,
    snapshot_reader_contract_version: int = SNAPSHOT_READER_CONTRACT_VERSION,
) -> Path:
    """Create a complete STAGED bundle below the managed root.

    The stage is internal job state only; callers must not create a release
    record until :func:`register_release_stage` succeeds.
    """

    if not app_version or not git_revision:
        raise ReleaseAssetError("app version and git revision are required")
    _validate_official_release_identity(
        app_version=app_version, git_revision=git_revision
    )
    if package_version is not None and package_version != app_version:
        raise ReleaseAssetError("package version and manifest app version do not match")
    root = _managed_root(managed_root)
    for source_value in (app_source, runtime_source):
        source = Path(source_value).resolve()
        try:
            root.relative_to(source)
        except ValueError:
            pass
        else:
            raise ReleaseAssetError(
                "managed root must not be nested inside a release source tree"
            )
    stage = root / "staging" / f"release-{uuid.uuid4().hex}"
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.mkdir(exist_ok=False)
    try:
        _copy_tree_create_only(Path(app_source), stage / "app")
        _copy_tree_create_only(Path(runtime_source), stage / "runtime")
        _write_canonical_json(stage / "runtime-profile.json", runtime_profile)
        _write_canonical_json(stage / "runner.json", runner)

        object_hashes = _object_hash_payload(stage)
        _write_canonical_json(stage / "object-hashes.json", object_hashes)
        runner_contract_version = int(runner.get("contract_version", 0))
        identity = _release_identity_body(
            app_version=app_version,
            git_revision=git_revision,
            build_digest=canonical_tree_digest(stage / "app"),
            runtime_bundle_digest=_sha256_bytes(_canonical_json_bytes(object_hashes)),
            runtime_profile_digest=_sha256_file(stage / "runtime-profile.json"),
            runner_contract_version=runner_contract_version,
            snapshot_reader_contract_version=int(snapshot_reader_contract_version),
        )
        _write_canonical_json(stage / "release-manifest.json", identity)
        validate_release_stage(stage)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


def validate_release_stage(stage_path: str | Path) -> ValidatedRelease:
    """Validate every required byte and return the immutable identity."""

    stage = Path(stage_path)
    if stage.is_symlink() or not stage.is_dir():
        raise ReleaseAssetError("release stage must be a real directory")
    actual_names = {path.name for path in stage.iterdir()}
    if actual_names != _BUNDLE_NAMES:
        raise ReleaseAssetError("release stage does not contain the exact bundle layout")
    if not canonical_tree_entries(stage / "app"):
        raise ReleaseAssetError("app package must contain at least one file")
    if not canonical_tree_entries(stage / "runtime"):
        raise ReleaseAssetError("runtime bundle must contain at least one file")

    expected_hashes = _object_hash_payload(stage)
    stored_hashes = _read_json_mapping(stage / "object-hashes.json")
    if stored_hashes != expected_hashes:
        raise ReleaseAssetError("object hash validation failed")

    manifest = _read_json_mapping(stage / "release-manifest.json")
    expected_keys = set(
        _release_identity_body(
            app_version="",
            git_revision="",
            build_digest="",
            runtime_bundle_digest="",
            runtime_profile_digest="",
            runner_contract_version=0,
            snapshot_reader_contract_version=0,
        )
    )
    if set(manifest) != expected_keys:
        raise ReleaseAssetError("release manifest fields do not match schema version 1")
    runner = _read_json_mapping(stage / "runner.json")
    runtime_profile = _read_json_mapping(stage / "runtime-profile.json")
    _validate_runtime_profile(runtime_profile)
    _runner_command(
        runner.get("command", []),
        bundle_root=stage,
        snapshot_root=stage / "validation-snapshot",
        workspace=stage / "validation-workspace",
        artifact_path=stage / "validation-result.json",
        python_executable=sys.executable,
    )
    try:
        expected_manifest = _release_identity_body(
            app_version=str(manifest["app_version"]),
            git_revision=str(manifest["git_revision"]),
            build_digest=canonical_tree_digest(stage / "app"),
            runtime_bundle_digest=_sha256_bytes(_canonical_json_bytes(stored_hashes)),
            runtime_profile_digest=_sha256_file(stage / "runtime-profile.json"),
            runner_contract_version=int(runner["contract_version"]),
            snapshot_reader_contract_version=int(
                manifest["snapshot_reader_contract_version"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseAssetError("release manifest has invalid typed fields") from exc
    if manifest != expected_manifest:
        raise ReleaseAssetError("release manifest digest or contract validation failed")
    if int(manifest["schema_version"]) != RELEASE_SCHEMA_VERSION:
        raise ReleaseAssetError("unsupported release manifest schema")
    if int(manifest["runner_contract_version"]) <= 0:
        raise ReleaseAssetError("runner contract version must be positive")
    if int(manifest["snapshot_reader_contract_version"]) <= 0:
        raise ReleaseAssetError("snapshot reader contract version must be positive")

    identity = _sha256_bytes(_canonical_json_bytes(manifest))
    return ValidatedRelease(
        stage_path=stage,
        release_manifest_id=identity,
        app_version=str(manifest["app_version"]),
        git_revision=str(manifest["git_revision"]),
        build_digest=str(manifest["build_digest"]),
        runtime_bundle_digest=str(manifest["runtime_bundle_digest"]),
        runtime_profile_digest=str(manifest["runtime_profile_digest"]),
        runner_contract_version=int(manifest["runner_contract_version"]),
        snapshot_reader_contract_version=int(
            manifest["snapshot_reader_contract_version"]
        ),
    )


def assert_version_digest_compatible(
    app_version: str,
    incoming_release_manifest_id: str,
    existing_release_manifest_id: str | None,
) -> None:
    """Reject reusing one version label for different immutable bytes."""

    if (
        existing_release_manifest_id is not None
        and incoming_release_manifest_id != existing_release_manifest_id
    ):
        raise ReleaseAssetError(
            f"release {app_version} already exists with a different digest"
        )


def _descriptor(validated: ValidatedRelease, path: Path) -> ReleaseDescriptor:
    return ReleaseDescriptor(
        release_manifest_id=validated.release_manifest_id,
        app_version=validated.app_version,
        git_revision=validated.git_revision,
        build_digest=validated.build_digest,
        runtime_bundle_digest=validated.runtime_bundle_digest,
        runtime_profile_digest=validated.runtime_profile_digest,
        runner_contract_version=validated.runner_contract_version,
        snapshot_reader_contract_version=validated.snapshot_reader_contract_version,
        path=path,
    )


def register_release_stage(
    managed_root: str | Path,
    stage_path: str | Path,
    *,
    expected_tag_version: str,
    expected_git_revision: str | None = None,
    existing_release_manifest_id: str | None = None,
) -> ReleaseDescriptor:
    """Atomically publish a validated STAGED bundle as a create-only object."""

    root = _managed_root(managed_root)
    stage = _contained_path(stage_path, managed_root=root)
    try:
        if stage.relative_to(root).parts[0] != "staging":
            raise ReleaseAssetError("release stage must be below managed staging")
    except ValueError as exc:  # defensive; containment was already checked
        raise ReleaseAssetError("release stage escapes managed staging") from exc
    validated = validate_release_stage(stage)
    _validate_official_release_identity(
        app_version=validated.app_version,
        git_revision=validated.git_revision,
    )
    normalized_tag = expected_tag_version.removeprefix("v")
    if normalized_tag != validated.app_version:
        raise ReleaseAssetError("release tag and manifest app version do not match")
    if expected_git_revision is not None and expected_git_revision != validated.git_revision:
        raise ReleaseAssetError("release git revision does not match expected revision")
    assert_version_digest_compatible(
        validated.app_version,
        validated.release_manifest_id,
        existing_release_manifest_id,
    )

    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / validated.release_manifest_id
    if target.exists():
        existing = validate_release_stage(target)
        if existing.release_manifest_id != validated.release_manifest_id:
            raise ReleaseAssetError("immutable release target contains different bytes")
        safe_cleanup(stage, managed_root=root)
        return _descriptor(existing, target)
    os.replace(stage, target)
    return _descriptor(validated, target)


def inspect_release(
    descriptor: ReleaseDescriptor,
    *,
    expected_runner_contract_version: int = RUNNER_CONTRACT_VERSION,
    expected_snapshot_reader_contract_version: int = SNAPSHOT_READER_CONTRACT_VERSION,
) -> ReleaseAvailability:
    """Derive local availability without changing the registered identity."""

    if not descriptor.path.exists():
        return ReleaseAvailability.LOCAL_MISSING
    try:
        actual = validate_release_stage(descriptor.path)
    except ReleaseAssetError:
        return ReleaseAvailability.CORRUPT
    if actual.release_manifest_id != descriptor.release_manifest_id:
        return ReleaseAvailability.CORRUPT
    if (
        actual.runner_contract_version != expected_runner_contract_version
        or actual.snapshot_reader_contract_version
        != expected_snapshot_reader_contract_version
    ):
        return ReleaseAvailability.INCOMPATIBLE
    return ReleaseAvailability.AVAILABLE


def copy_release_bundle(source: str | Path, target: str | Path) -> Path:
    """Make an exact create-only backup copy of a validated bundle."""

    source_path = Path(source)
    validate_release_stage(source_path)
    target_path = Path(target)
    _copy_tree_create_only(source_path, target_path)
    return target_path


def restore_release_bundle(
    managed_root: str | Path,
    descriptor: ReleaseDescriptor,
    source_bundle: str | Path,
) -> Path:
    """Restore missing registered bytes only when their identity is exact."""

    root = _managed_root(managed_root)
    target = _contained_path(descriptor.path, managed_root=root)
    if target.exists():
        raise ReleaseAssetError("immutable release target already exists; refusing overwrite")
    source = Path(source_bundle)
    try:
        incoming = validate_release_stage(source)
    except ReleaseAssetError as exc:
        raise ReleaseAssetError(
            "restore source has different bytes from registered release"
        ) from exc
    if incoming.release_manifest_id != descriptor.release_manifest_id:
        raise ReleaseAssetError("restore source has different bytes from registered release")

    temporary = root / "staging" / f"restore-{uuid.uuid4().hex}"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    try:
        _copy_tree_create_only(source, temporary)
        copied = validate_release_stage(temporary)
        if copied.release_manifest_id != descriptor.release_manifest_id:
            raise ReleaseAssetError("restored copy changed immutable release bytes")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def safe_cleanup(
    path: str | Path,
    *,
    managed_root: str | Path,
    process: subprocess.Popen[str] | Any | None = None,
) -> None:
    """Delete one contained object only after its associated process stopped."""

    target = _contained_path(path, managed_root=managed_root)
    if process is not None and process.poll() is None:
        raise ReleaseAssetError("refusing cleanup for a running process")
    if target.is_symlink():
        raise ReleaseAssetError("refusing cleanup of a symbolic link")
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _runner_command(
    command: Sequence[Any],
    *,
    bundle_root: Path,
    snapshot_root: Path,
    workspace: Path,
    artifact_path: Path,
    python_executable: str,
) -> list[str]:
    if (
        len(command) < 2
        or not all(isinstance(value, str) and value for value in command)
    ):
        raise ReleaseAssetError("runner command must be a non-empty string array")
    substitutions = {
        "python": python_executable,
        "bundle_root": str(bundle_root),
        "app_root": str(bundle_root / "app"),
        "runtime_root": str(bundle_root / "runtime"),
        "snapshot_root": str(snapshot_root),
        "workspace": str(workspace),
        "artifact_path": str(artifact_path),
    }
    try:
        rendered = [value.format_map(substitutions) for value in command]
    except (KeyError, ValueError) as exc:
        raise ReleaseAssetError("runner command contains an unsupported placeholder") from exc
    if rendered[0] != python_executable:
        raise ReleaseAssetError("runner command must use the selected Python executable")
    try:
        entrypoint = Path(rendered[1]).resolve(strict=True)
    except OSError as exc:
        raise ReleaseAssetError("runner entrypoint is not available") from exc
    allowed_roots = (
        (bundle_root / "app").resolve(strict=True),
        (bundle_root / "runtime").resolve(strict=True),
    )
    if entrypoint.is_symlink() or not entrypoint.is_file() or not any(
        entrypoint.is_relative_to(root) for root in allowed_roots
    ):
        raise ReleaseAssetError(
            "runner entrypoint must be a registered app or runtime file"
        )
    return rendered


def execute_registered_release(
    managed_root: str | Path,
    descriptor: ReleaseDescriptor,
    *,
    snapshot_root: str | Path,
    run_id: str,
    input_payload: Mapping[str, Any] | None = None,
    python_executable: str | None = None,
    timeout_seconds: float = 300.0,
    extra_environment: Mapping[str, str] | None = None,
) -> ReleaseExecutionResult:
    """Execute registered bytes against exactly one managed fixed snapshot.

    The runner writes its result inside a disposable workspace.  That artifact
    is copied create-only to ``run-artifacts`` before cleanup is attempted.
    """

    root = _managed_root(managed_root)
    release_path = _contained_path(descriptor.path, managed_root=root)
    if inspect_release(descriptor) is not ReleaseAvailability.AVAILABLE:
        raise ReleaseAssetError("registered release is not available for execution")
    snapshot = _contained_path(snapshot_root, managed_root=root)
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ReleaseAssetError("fixed snapshot path must be a managed real directory")
    if not artifact_io.is_safe_artifact_identifier(run_id):
        raise ReleaseAssetError("run id is not a safe immutable artifact identifier")

    workspace = root / "workspaces" / run_id
    execution_bundle = workspace / "release-bundle"
    workspace_artifact = workspace / "result.json"
    input_path = workspace / "input.json"

    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _INHERITED_PROCESS_ENVIRONMENT
    }
    for key, value in (extra_environment or {}).items():
        normalized_key = str(key).upper()
        if normalized_key not in _EXPLICIT_RUNNER_ENVIRONMENT:
            raise ReleaseAssetError(
                f"runner environment key is not allowed: {key}"
            )
        if not isinstance(value, str) or "\x00" in value:
            raise ReleaseAssetError(
                f"runner environment value is invalid: {key}"
            )
        environment[normalized_key] = value
    environment.update(
        {
            "FINANCE_LLM_RELEASE_BUNDLE_ROOT": str(execution_bundle),
            "FINANCE_LLM_RELEASE_APP_ROOT": str(execution_bundle / "app"),
            "FINANCE_LLM_RELEASE_RUNTIME_ROOT": str(execution_bundle / "runtime"),
            "FINANCE_LLM_FIXED_SNAPSHOT_ROOT": str(snapshot),
            "FINANCE_LLM_RUN_WORKSPACE": str(workspace),
            "FINANCE_LLM_RUN_INPUT_PATH": str(input_path),
            "FINANCE_LLM_RUN_ARTIFACT_PATH": str(workspace_artifact),
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    workspace.parent.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(exist_ok=False)
    try:
        with (workspace / ".env").open("xb"):
            pass
        _copy_tree_create_only(release_path, execution_bundle)
        copied_release = validate_release_stage(execution_bundle)
        if copied_release.release_manifest_id != descriptor.release_manifest_id:
            raise ReleaseAssetError("execution copy changed immutable release bytes")
        runner = _read_json_mapping(execution_bundle / "runner.json")
        artifact_relative = Path(
            str(runner.get("artifact_relative_path", "result.json"))
        )
        if artifact_relative.is_absolute() or ".." in artifact_relative.parts:
            raise ReleaseAssetError(
                "runner artifact path must remain inside its workspace"
            )
        workspace_artifact = workspace / artifact_relative
        command = _runner_command(
            runner.get("command", []),
            bundle_root=execution_bundle,
            snapshot_root=snapshot,
            workspace=workspace,
            artifact_path=workspace_artifact,
            python_executable=python_executable or sys.executable,
        )
        environment["FINANCE_LLM_RUN_ARTIFACT_PATH"] = str(workspace_artifact)
        _prepare_fixed_snapshot_compatibility(snapshot, workspace)
        workspace_artifact.parent.mkdir(parents=True, exist_ok=True)
        _write_canonical_json(input_path, input_payload or {})
    except BaseException:
        try:
            safe_cleanup(workspace, managed_root=root)
        except (OSError, ReleaseAssetError):
            pass
        raise
    process: subprocess.Popen[str] | None = None
    saved_artifact: Path | None = None
    artifact_digest: str | None = None
    stdout = ""
    stderr = ""
    returncode = -1
    cleanup_warning: str | None = None
    execution_error: BaseException | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            raise ReleaseAssetError("registered release execution timed out") from exc
        returncode = int(process.returncode)
        if not workspace_artifact.is_file() or workspace_artifact.is_symlink():
            raise ReleaseAssetError(
                "registered runner completed without a regular run artifact"
            )
        artifact_bytes = workspace_artifact.read_bytes()
        artifact_digest = _sha256_bytes(artifact_bytes)
        suffix = workspace_artifact.suffix or ".bin"
        artifact_directory = root / "run-artifacts" / run_id
        artifact_directory.mkdir(parents=True, exist_ok=False)
        saved_artifact = artifact_directory / f"{artifact_digest}{suffix}"
        with saved_artifact.open("xb") as handle:
            handle.write(artifact_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException as exc:
        execution_error = exc
    finally:
        try:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        except BaseException as exc:
            cleanup_warning = f"process termination required: {exc}"
        try:
            safe_cleanup(workspace, managed_root=root, process=process)
        except (OSError, ReleaseAssetError) as exc:
            warning = f"workspace cleanup required: {exc}"
            cleanup_warning = (
                f"{cleanup_warning}; {warning}" if cleanup_warning else warning
            )

    if execution_error is not None:
        if cleanup_warning:
            raise ReleaseAssetError(
                f"{execution_error}; {cleanup_warning}"
            ) from execution_error
        raise execution_error
    assert saved_artifact is not None and artifact_digest is not None
    return ReleaseExecutionResult(
        returncode=returncode,
        artifact_path=saved_artifact,
        artifact_digest=artifact_digest,
        workspace_path=workspace,
        stdout=stdout,
        stderr=stderr,
        cleanup_warning=cleanup_warning,
    )
