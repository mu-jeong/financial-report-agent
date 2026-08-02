"""Dependency-free contracts for immutable evaluation bundle material.

This module intentionally contains no capture, job, locking, or provider logic.  It
defines the portable JSON boundary used by those later layers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from src.core.artifact_io import (
    contains_sensitive_identifier_pattern,
    is_safe_artifact_identifier,
)


SCHEMA_VERSION = 1
ARTIFACT_TYPES = frozenset(
    {
        "capture_receipt",
        "evaluation_dataset",
        "evaluation_bundle_manifest",
        "snapshot_reference",
        "validation",
        "trial_run",
        "trial_run_receipt",
        "approval",
        "seal",
        "active_evaluation_bundle",
    }
)

_SHA256_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")
_MANIFEST_BASE_FILES = {
    "evaluation_dataset": "evaluation_dataset.json",
    "capture_receipt": "capture_receipt.json",
    "snapshot_reference": "snapshot_reference.json",
    "reports_database": "data/reports.db",
    "catalog_database": "data/retrieval/v2/catalog.sqlite3",
}
_EMBEDDING_COMPONENTS = {
    "model",
    "dimension",
    "distance_metric",
    "normalization",
    "text_format",
    "document_extraction",
    "parent_chunking",
    "child_chunking",
}
_MAX_SHORT_TEXT = 256
_MAX_EVIDENCE_TEXT = 512
_MAX_STRUCTURED_ITEMS = 1_000
_MAX_CHECKS_PER_TURN = 256
_MAX_FILTERS_PER_TURN = 64
_MAX_SOURCES_PER_TURN = 256
_MAX_FACTS_PER_TURN = 256
_MAX_TRIAL_PAIRS = 10_000
_MAX_TRIAL_JSON_BYTES = 2 * 1024 * 1024
_MAX_TRIAL_RECEIPT_JSON_BYTES = 64 * 1024
_ACTIVE_HISTORY_KEYS = frozenset(
    {
        "active",
        "active_bundle_id",
        "active_revision",
        "activated_at",
        "activation",
        "activation_history",
        "previous_active_bundle_id",
        "selection_revision",
        "selected_at",
    }
)


class EvaluationBundleError(ValueError):
    """Raised when evaluation bundle material violates its public contract."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the stable UTF-8 JSON representation used for content identities."""

    _validate_json_value(value, path="$", seen=set())
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationBundleError("value is not canonical JSON material") from exc


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 identity of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def artifact_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the exact bytes written by ``artifact_io.atomic_write_json``."""

    _validate_json_value(value, path="$", seen=set())
    try:
        return (
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EvaluationBundleError("value is not persisted JSON material") from exc


def artifact_json_sha256(value: Mapping[str, Any]) -> str:
    """Return the SHA-256 of the pretty JSON artifact bytes persisted on disk."""

    return hashlib.sha256(artifact_json_bytes(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Hash the bytes of a regular, non-symlink file."""

    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise EvaluationBundleError("file hash target must be a regular non-symlink file")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_safe_identifier(value: Any, *, field: str = "identifier") -> str:
    """Validate an opaque identifier that is also safe as one path component."""

    if not isinstance(value, str) or not is_safe_artifact_identifier(value):
        raise EvaluationBundleError(f"{field} is not a safe artifact identifier")
    return value


def validate_relative_path(value: Any, *, field: str = "path") -> str:
    """Validate and normalize a slash-separated, non-traversing relative path."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise EvaluationBundleError(f"{field} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise EvaluationBundleError(f"{field} must be a normalized relative path")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise EvaluationBundleError(f"{field} contains an unsafe path component")
    if path.parts and path.parts[0].endswith(":"):
        raise EvaluationBundleError(f"{field} must not be a drive path")
    return value


def resolve_safe_relative_path(
    root: str | Path,
    relative_path: str,
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve a validated child path and reject every existing symlink component."""

    relative = validate_relative_path(relative_path)
    root_path = Path(root)
    if root_path.is_symlink():
        raise EvaluationBundleError("root must not be a symbolic link")
    root_resolved = root_path.resolve(strict=must_exist)
    current = root_path
    for component in PurePosixPath(relative).parts:
        current = current / component
        if current.is_symlink():
            raise EvaluationBundleError(f"path contains a symbolic link: {relative}")
    if must_exist and not current.exists():
        raise EvaluationBundleError(f"path does not exist: {relative}")
    candidate = current.resolve(strict=must_exist)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise EvaluationBundleError("path escapes its allowed root") from exc
    return candidate


def normalize_evaluation_dataset(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated dataset with every case represented by ordered ``turns``.

    A case may use the convenience ``turn`` member for a single turn.  The returned
    material always uses ``turns``; persisted material therefore has one schema for
    both single-turn and multi-turn evaluation.
    """

    result = _copy_mapping(dataset, "evaluation dataset")
    _require_exact_common(result, "evaluation_dataset")
    _require_exact_keys(
        result,
        {"schema_version", "artifact_type", "dataset_id", "data_revision", "cases"},
        "dataset",
    )
    validate_safe_identifier(result["dataset_id"], field="dataset_id")
    _require_non_empty_string(result["data_revision"], "data_revision")
    cases = result["cases"]
    if not _is_sequence(cases) or not cases:
        raise EvaluationBundleError("cases must be a non-empty array")

    normalized_cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for case_index, raw_case in enumerate(cases):
        case = _copy_mapping(raw_case, f"cases[{case_index}]")
        _require_keys(case, {"case_id"}, f"cases[{case_index}]")
        case_id = validate_safe_identifier(case["case_id"], field="case_id")
        if case_id in case_ids:
            raise EvaluationBundleError(f"duplicate case_id: {case_id}")
        case_ids.add(case_id)
        has_turn = "turn" in case
        has_turns = "turns" in case
        if has_turn == has_turns:
            raise EvaluationBundleError(
                f"case {case_id} must contain exactly one of turn or turns"
            )
        _require_exact_keys(
            case,
            {"case_id", "turn"} if has_turn else {"case_id", "turns"},
            f"cases[{case_index}]",
        )
        raw_turns = [case.pop("turn")] if has_turn else case["turns"]
        if not _is_sequence(raw_turns) or not raw_turns:
            raise EvaluationBundleError(f"case {case_id} must have at least one turn")
        turns: list[dict[str, Any]] = []
        turn_ids: set[str] = set()
        for turn_index, raw_turn in enumerate(raw_turns):
            turn = _copy_mapping(raw_turn, f"case {case_id} turn {turn_index}")
            _require_exact_keys(turn, {"turn_id", "question", "expectations"}, "turn")
            turn_id = validate_safe_identifier(turn["turn_id"], field="turn_id")
            if turn_id in turn_ids:
                raise EvaluationBundleError(
                    f"duplicate turn_id {turn_id} in case {case_id}"
                )
            turn_ids.add(turn_id)
            _require_bounded_string(turn["question"], "question", _MAX_EVIDENCE_TEXT)
            if not isinstance(turn["expectations"], Mapping):
                raise EvaluationBundleError("turn expectations must be an object")
            _validate_turn_expectations(turn["expectations"])
            turns.append(turn)
        case["turns"] = turns
        normalized_cases.append(case)
    result["cases"] = normalized_cases
    return result


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the immutable manifest contract and return a detached copy."""

    result = _copy_mapping(manifest, "manifest")
    _require_exact_common(result, "evaluation_bundle_manifest")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "bundle_id",
            "created_at",
            "actor",
            "dataset",
            "source_snapshot",
            "revisions",
            "embedding_profile",
            "statistics",
            "base_files",
            "snapshot_reference_hash",
            "dataset_set_hash",
            "execution_policy",
            "storage_policy",
        },
        "manifest",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    _require_non_empty_string(result["created_at"], "created_at")
    _require_non_empty_string(result["actor"], "actor")
    for field in (
        "dataset",
        "source_snapshot",
        "revisions",
        "embedding_profile",
        "statistics",
    ):
        if not isinstance(result[field], Mapping):
            raise EvaluationBundleError(f"manifest {field} must be an object")
    dataset = result["dataset"]
    _require_exact_keys(
        dataset,
        {"path", "version", "case_count", "turn_count", "sha256"},
        "dataset",
    )
    validate_relative_path(dataset["path"], field="dataset.path")
    if dataset["path"] != "evaluation_dataset.json":
        raise EvaluationBundleError("dataset.path must be evaluation_dataset.json")
    _require_non_empty_string(dataset["version"], "dataset.version")
    _validate_sha256(dataset["sha256"], "dataset.sha256")
    for field in ("case_count", "turn_count"):
        if not isinstance(dataset[field], int) or isinstance(dataset[field], bool) or dataset[field] < 1:
            raise EvaluationBundleError(f"dataset.{field} must be a positive integer")
    source = result["source_snapshot"]
    _require_exact_keys(
        source,
        {
            "retrieval_id",
            "snapshot_id",
            "build_id",
            "publication_generation",
            "write_generation",
        },
        "source_snapshot",
    )
    for field in ("retrieval_id", "snapshot_id", "build_id"):
        validate_safe_identifier(source[field], field=f"source_snapshot.{field}")
    for field in ("publication_generation", "write_generation"):
        _validate_positive_integer(source[field], f"source_snapshot.{field}")
    revisions = result["revisions"]
    _require_exact_keys(revisions, {"data_revision", "index_revision"}, "revisions")
    for field in ("data_revision", "index_revision"):
        validate_safe_identifier(revisions[field], field=f"revisions.{field}")
    profile = result["embedding_profile"]
    _require_exact_keys(profile, {"profile_id", "sha256", "components"}, "embedding_profile")
    validate_safe_identifier(profile["profile_id"], field="profile_id")
    _validate_sha256(profile["sha256"], "embedding_profile.sha256")
    if not isinstance(profile["components"], Mapping):
        raise EvaluationBundleError("embedding_profile.components must be an object")
    _require_exact_keys(profile["components"], _EMBEDDING_COMPONENTS, "embedding_profile.components")
    for field, value in profile["components"].items():
        if field == "dimension":
            _validate_positive_integer(value, "embedding_profile.components.dimension")
        else:
            _require_non_empty_string(value, f"embedding_profile.components.{field}")
    statistics = result["statistics"]
    _require_exact_keys(
        statistics,
        {
            "document_count",
            "parent_chunk_count",
            "child_chunk_count",
            "vector_count",
            "data_date_start",
            "data_date_end",
        },
        "statistics",
    )
    for field in ("document_count", "parent_chunk_count", "child_chunk_count", "vector_count"):
        if not isinstance(statistics[field], int) or isinstance(statistics[field], bool) or statistics[field] < 0:
            raise EvaluationBundleError(f"statistics.{field} must be a non-negative integer")
    for field in ("data_date_start", "data_date_end"):
        _require_non_empty_string(statistics[field], f"statistics.{field}")
    files = result["base_files"]
    if not _is_sequence(files) or not files:
        raise EvaluationBundleError("base_files must be a non-empty array")
    seen_paths: set[str] = set()
    seen_roles: set[str] = set()
    for index, entry in enumerate(files):
        _validate_file_description(entry, f"base_files[{index}]", seen_paths)
        role = entry["role"]
        if role not in _MANIFEST_BASE_FILES:
            raise EvaluationBundleError(f"base_files[{index}].role is not allowed")
        if role in seen_roles:
            raise EvaluationBundleError(f"duplicate base file role: {role}")
        seen_roles.add(role)
        if entry["path"] != _MANIFEST_BASE_FILES[role]:
            raise EvaluationBundleError(f"base_files[{index}] path does not match its role")
    if seen_roles != set(_MANIFEST_BASE_FILES):
        raise EvaluationBundleError("base_files must contain every required foundation artifact")
    _validate_sha256(result["snapshot_reference_hash"], "snapshot_reference_hash")
    _validate_sha256(result["dataset_set_hash"], "dataset_set_hash")
    execution_policy = result["execution_policy"]
    if not isinstance(execution_policy, Mapping):
        raise EvaluationBundleError("execution_policy must be an object")
    _require_exact_keys(
        execution_policy,
        {"data_and_index_immutable", "model_values_recorded_at_runtime"},
        "execution_policy",
    )
    if execution_policy != {
        "data_and_index_immutable": True,
        "model_values_recorded_at_runtime": True,
    }:
        raise EvaluationBundleError("execution_policy must declare the immutable/runtime policy")
    storage_policy = result["storage_policy"]
    if not isinstance(storage_policy, Mapping):
        raise EvaluationBundleError("storage_policy must be an object")
    _require_exact_keys(
        storage_policy,
        {"single_machine_only", "automatic_backup"},
        "storage_policy",
    )
    if storage_policy != {"single_machine_only": True, "automatic_backup": False}:
        raise EvaluationBundleError("storage_policy must declare local-only storage without backup")
    _reject_keys_recursive(result, _ACTIVE_HISTORY_KEYS, "manifest")
    return result


def validate_capture_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the completed logical-capture receipt without performing capture."""

    result = _copy_mapping(receipt, "capture receipt")
    _require_exact_common(result, "capture_receipt")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "bundle_id",
            "source_snapshot",
            "started_at",
            "finished_at",
            "copied_files",
            "referenced_files",
            "temporary_pin_id",
            "reference_set_id",
            "pin_verified",
            "complete",
        },
        "capture receipt",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    validate_safe_identifier(result["temporary_pin_id"], field="temporary_pin_id")
    validate_safe_identifier(result["reference_set_id"], field="reference_set_id")
    for field in ("started_at", "finished_at"):
        _require_non_empty_string(result[field], field)
    if not isinstance(result["source_snapshot"], Mapping):
        raise EvaluationBundleError("source_snapshot must be an object")
    _require_exact_keys(
        result["source_snapshot"],
        {"snapshot_id", "build_id", "publication_generation"},
        "source_snapshot",
    )
    for field in ("snapshot_id", "build_id"):
        validate_safe_identifier(result["source_snapshot"][field], field=field)
    _validate_positive_integer(
        result["source_snapshot"]["publication_generation"],
        "publication_generation",
    )
    seen_paths: set[str] = set()
    for field in ("copied_files", "referenced_files"):
        entries = result[field]
        if not _is_sequence(entries):
            raise EvaluationBundleError(f"{field} must be an array")
        for index, entry in enumerate(entries):
            _validate_file_description(entry, f"{field}[{index}]", seen_paths)
    if not result["copied_files"]:
        raise EvaluationBundleError("copied_files must be a non-empty array")
    for field in ("pin_verified", "complete"):
        if not isinstance(result[field], bool):
            raise EvaluationBundleError(f"{field} must be a boolean")
    if result["complete"] and not result["pin_verified"]:
        raise EvaluationBundleError("a complete capture receipt requires a verified pin")
    if result["complete"] and not result["referenced_files"]:
        raise EvaluationBundleError("a complete V2 capture receipt requires referenced_files")
    return result


def validate_snapshot_reference(reference: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable reference descriptions without opening referenced files."""

    result = _copy_mapping(reference, "snapshot reference")
    _require_exact_common(result, "snapshot_reference")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "source_root_id",
            "snapshot_id",
            "build_id",
            "publication_generation",
            "files",
            "publication_validation_files",
            "checkpoint_files",
            "reference_set_id",
            "reference_set_hash",
        },
        "snapshot reference",
    )
    for field in (
        "source_root_id",
        "snapshot_id",
        "build_id",
        "reference_set_id",
    ):
        validate_safe_identifier(result[field], field=field)
    _validate_positive_integer(result["publication_generation"], "publication_generation")
    _validate_sha256(result["reference_set_hash"], "reference_set_hash")
    seen_paths: set[str] = set()
    described_paths: set[str] = set()
    for index, entry in enumerate(result["files"] if _is_sequence(result["files"]) else []):
        _validate_file_description(entry, f"files[{index}]", seen_paths)
        described_paths.add(entry["path"])
    if not _is_sequence(result["files"]) or not result["files"]:
        raise EvaluationBundleError("files must be a non-empty array")
    for field in ("publication_validation_files", "checkpoint_files"):
        paths = result[field]
        if not _is_sequence(paths):
            raise EvaluationBundleError(f"{field} must be an array")
        for index, path in enumerate(paths):
            normalized = validate_relative_path(path, field=f"{field}[{index}]")
            if normalized not in described_paths:
                raise EvaluationBundleError(
                    f"{field}[{index}] is not present in the reference file descriptions"
                )
    _reject_keys_recursive(
        result,
        {"pin", "pin_file", "pin_filename", "temporary_pin", "final_pin"},
        "snapshot reference",
    )
    return result


def validate_validation_result(validation: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a stored deterministic validation result and its identity links."""

    result = _copy_mapping(validation, "validation result")
    _require_exact_common(result, "validation")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "bundle_id",
            "manifest_hash",
            "dataset_set_hash",
            "snapshot_reference_hash",
            "reference_set_id",
            "expected_case_count",
            "expected_turn_count",
            "validated_case_count",
            "validated_turn_count",
            "checks",
            "issues",
            "status",
            "validated_at",
        },
        "validation result",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    validate_safe_identifier(result["reference_set_id"], field="reference_set_id")
    for field in ("manifest_hash", "dataset_set_hash", "snapshot_reference_hash"):
        _validate_sha256(result[field], field)
    for field in (
        "expected_case_count",
        "expected_turn_count",
        "validated_case_count",
        "validated_turn_count",
    ):
        if not isinstance(result[field], int) or isinstance(result[field], bool) or result[field] < 0:
            raise EvaluationBundleError(f"{field} must be a non-negative integer")
    if result["validated_case_count"] > result["expected_case_count"]:
        raise EvaluationBundleError("validated_case_count exceeds expected_case_count")
    if result["validated_turn_count"] > result["expected_turn_count"]:
        raise EvaluationBundleError("validated_turn_count exceeds expected_turn_count")
    if not _is_sequence(result["checks"]) or not _is_sequence(result["issues"]):
        raise EvaluationBundleError("checks and issues must be arrays")
    validation_check_ids: set[str] = set()
    for index, raw_check in enumerate(result["checks"]):
        check = _copy_mapping(raw_check, f"checks[{index}]")
        _require_exact_keys(check, {"check_id", "passed"}, f"checks[{index}]")
        check_id = validate_safe_identifier(check["check_id"], field="check_id")
        if check_id in validation_check_ids:
            raise EvaluationBundleError("validation checks contain a duplicate check_id")
        validation_check_ids.add(check_id)
        if not isinstance(check["passed"], bool):
            raise EvaluationBundleError("validation check passed must be a boolean")
    for index, raw_issue in enumerate(result["issues"]):
        issue = _copy_mapping(raw_issue, f"issues[{index}]")
        _require_exact_keys(issue, {"code", "message"}, f"issues[{index}]")
        validate_safe_identifier(issue["code"], field="issue.code")
        _require_bounded_string(issue["message"], "issue.message", _MAX_EVIDENCE_TEXT)
    if result["status"] not in {"passed", "failed"}:
        raise EvaluationBundleError("validation status is invalid")
    if result["status"] == "passed" and (
        result["issues"]
        or any(not check["passed"] for check in result["checks"])
        or result["validated_case_count"] != result["expected_case_count"]
        or result["validated_turn_count"] != result["expected_turn_count"]
    ):
        raise EvaluationBundleError("passed validation must be complete and issue-free")
    _require_non_empty_string(result["validated_at"], "validated_at")
    return result


def validate_trial_run(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Validate redacted, structured trial evidence."""

    _validate_artifact_size(trial, "trial run", _MAX_TRIAL_JSON_BYTES)
    result = _copy_mapping(trial, "trial run")
    _require_exact_common(result, "trial_run")
    fields = {
        "schema_version",
        "artifact_type",
        "bundle_id",
        "content_hash",
        "validation_hash",
        "case_turns",
        "evidence",
        "usage",
        "completed_case_count",
        "completed_turn_count",
        "expected_case_count",
        "expected_turn_count",
        "started_at",
        "finished_at",
        "process_exit_status",
        "status",
        "reproduction",
        "reproduction_hash",
        "models",
    }
    _require_exact_keys(
        result,
        fields,
        "trial run",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    for field in ("content_hash", "validation_hash", "reproduction_hash"):
        _validate_sha256(result[field], field)
    if not _is_sequence(result["case_turns"]) or not result["case_turns"]:
        raise EvaluationBundleError("case_turns must be a non-empty array")
    if len(result["case_turns"]) > _MAX_TRIAL_PAIRS:
        raise EvaluationBundleError("case_turns exceeds its item limit")
    expected_pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(result["case_turns"]):
        pair = _copy_mapping(item, f"case_turns[{index}]")
        _require_exact_keys(pair, {"case_id", "turn_id"}, f"case_turns[{index}]")
        case_id = validate_safe_identifier(pair["case_id"], field="case_id")
        turn_id = validate_safe_identifier(pair["turn_id"], field="turn_id")
        if (case_id, turn_id) in expected_pairs:
            raise EvaluationBundleError("case_turns contains a duplicate case/turn pair")
        expected_pairs.add((case_id, turn_id))
    if not _is_sequence(result["evidence"]):
        raise EvaluationBundleError("evidence must be an array")
    if len(result["evidence"]) > _MAX_TRIAL_PAIRS:
        raise EvaluationBundleError("evidence exceeds its item limit")
    evidence_pairs: set[tuple[str, str]] = set()
    evidence_items: list[dict[str, Any]] = []
    for index, raw_evidence in enumerate(result["evidence"]):
        item = _copy_mapping(raw_evidence, f"evidence[{index}]")
        _validate_trial_evidence(item, f"evidence[{index}]")
        pair = (item["case_id"], item["turn_id"])
        if pair not in expected_pairs or pair in evidence_pairs:
            raise EvaluationBundleError("evidence must map uniquely to a declared case/turn")
        evidence_pairs.add(pair)
        evidence_items.append(item)
    _validate_trial_usage(result["usage"])
    _validate_process_exit_status(result["process_exit_status"])
    reproduction = _copy_mapping(result["reproduction"], "reproduction")
    _require_exact_keys(reproduction, {"complete", "fingerprint"}, "reproduction")
    if not isinstance(reproduction["complete"], bool):
        raise EvaluationBundleError("reproduction.complete must be a boolean")
    _validate_sha256(reproduction["fingerprint"], "reproduction.fingerprint")
    models = _copy_mapping(result["models"], "models")
    _require_exact_keys(models, {"embedding", "generation", "reranking"}, "models")
    for field in ("embedding", "generation"):
        validate_safe_identifier(models[field], field=f"models.{field}")
    if models["reranking"] is not None:
        validate_safe_identifier(models["reranking"], field="models.reranking")
    for field in (
        "completed_case_count",
        "completed_turn_count",
        "expected_case_count",
        "expected_turn_count",
    ):
        if not isinstance(result[field], int) or isinstance(result[field], bool) or result[field] < 0:
            raise EvaluationBundleError(f"{field} must be a non-negative integer")
    if result["completed_case_count"] > result["expected_case_count"]:
        raise EvaluationBundleError("completed_case_count exceeds expected_case_count")
    if result["completed_turn_count"] > result["expected_turn_count"]:
        raise EvaluationBundleError("completed_turn_count exceeds expected_turn_count")
    if result["status"] not in {
        "cancelled",
        "execution_unavailable",
        "incomplete",
        "failed",
        "passed",
    }:
        raise EvaluationBundleError("trial status is invalid")
    if result["status"] == "passed":
        declared_case_count = len({case_id for case_id, _ in expected_pairs})
        if evidence_pairs != expected_pairs:
            raise EvaluationBundleError("passed trial evidence must cover every declared case/turn")
        if not (
            result["completed_case_count"]
            == result["expected_case_count"]
            == declared_case_count
            and result["completed_turn_count"]
            == result["expected_turn_count"]
            == len(expected_pairs)
        ):
            raise EvaluationBundleError("passed trial counts must equal declared case/turn counts")
        for item in evidence_items:
            if (
                not item["checks"]
                or any(not check["passed"] for check in item["checks"])
                or item["failed_checks"]
            ):
                raise EvaluationBundleError("passed trial requires every required check to pass")
            if item["semantic_review"]["status"] == "rejected":
                raise EvaluationBundleError("passed trial cannot have rejected semantic review")
        if not reproduction["complete"]:
            raise EvaluationBundleError("passed trial requires complete reproduction evidence")
        if result["process_exit_status"] != {"code": 0, "signal": None}:
            raise EvaluationBundleError("passed trial requires a successful process exit")
    for field in ("started_at", "finished_at"):
        _require_bounded_string(result[field], field, 64)
    _validate_no_sensitive_strings(result, "trial run")
    return result


def validate_trial_run_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the completion receipt that binds a trial result to its inputs."""

    _validate_artifact_size(
        receipt,
        "trial run receipt",
        _MAX_TRIAL_RECEIPT_JSON_BYTES,
    )
    result = _copy_mapping(receipt, "trial run receipt")
    _require_exact_common(result, "trial_run_receipt")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "bundle_id",
            "content_hash",
            "validation_hash",
            "trial_run_hash",
            "expected_case_count",
            "expected_turn_count",
            "completed_case_count",
            "completed_turn_count",
            "started_at",
            "finished_at",
            "process_exit_status",
            "status",
            "complete",
        },
        "trial run receipt",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    for field in ("content_hash", "validation_hash", "trial_run_hash"):
        _validate_sha256(result[field], field)
    for field in (
        "expected_case_count",
        "expected_turn_count",
        "completed_case_count",
        "completed_turn_count",
    ):
        if not isinstance(result[field], int) or isinstance(result[field], bool) or result[field] < 0:
            raise EvaluationBundleError(f"{field} must be a non-negative integer")
    if result["completed_case_count"] > result["expected_case_count"]:
        raise EvaluationBundleError("completed_case_count exceeds expected_case_count")
    if result["completed_turn_count"] > result["expected_turn_count"]:
        raise EvaluationBundleError("completed_turn_count exceeds expected_turn_count")
    for field in ("started_at", "finished_at"):
        _require_bounded_string(result[field], field, 64)
    _validate_process_exit_status(result["process_exit_status"])
    if result["status"] not in {
        "cancelled",
        "execution_unavailable",
        "incomplete",
        "failed",
        "passed",
    }:
        raise EvaluationBundleError("trial run receipt status is invalid")
    if not isinstance(result["complete"], bool):
        raise EvaluationBundleError("complete must be a boolean")
    if result["status"] == "passed" and (
        not result["complete"]
        or result["expected_case_count"] < 1
        or result["expected_turn_count"] < 1
        or result["completed_case_count"] != result["expected_case_count"]
        or result["completed_turn_count"] != result["expected_turn_count"]
        or result["process_exit_status"] != {"code": 0, "signal": None}
    ):
        raise EvaluationBundleError(
            "passed trial receipt requires positive complete counts and successful exit"
        )
    _validate_no_sensitive_strings(result, "trial run receipt")
    return result


def validate_approval(approval: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an approval linked to content, validation, and both trial files."""

    result = _copy_mapping(approval, "approval")
    _require_exact_common(result, "approval")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "bundle_id",
            "actor",
            "reason",
            "approved_at",
            "content_hash",
            "validation_hash",
            "trial_run_hash",
            "trial_run_receipt_hash",
        },
        "approval",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    for field in ("actor", "reason", "approved_at"):
        _require_bounded_string(result[field], field, _MAX_EVIDENCE_TEXT)
    for field in ("content_hash", "validation_hash", "trial_run_hash", "trial_run_receipt_hash"):
        _validate_sha256(result[field], field)
    return result


def validate_seal(seal: Mapping[str, Any]) -> dict[str, Any]:
    """Validate immutable seal links; activation history is deliberately forbidden."""

    result = _copy_mapping(seal, "seal")
    _require_exact_common(result, "seal")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "bundle_id",
            "manifest_hash",
            "dataset_set_hash",
            "snapshot_reference_hash",
            "validation_hash",
            "trial_run_hash",
            "trial_run_receipt_hash",
            "approval_hash",
            "sealed_at",
        },
        "seal",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    _require_non_empty_string(result["sealed_at"], "sealed_at")
    for field in (
        "manifest_hash",
        "dataset_set_hash",
        "snapshot_reference_hash",
        "validation_hash",
        "trial_run_hash",
        "trial_run_receipt_hash",
        "approval_hash",
    ):
        _validate_sha256(result[field], field)
    _reject_keys_recursive(result, _ACTIVE_HISTORY_KEYS | {"seal_hash"}, "seal")
    return result


def seal_hash(seal: Mapping[str, Any]) -> str:
    """Return the persisted-file identity of a valid immutable seal."""

    validate_seal(seal)
    return artifact_json_sha256(seal)


def validate_active_pointer(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """Validate mutable current-selection history kept outside a sealed bundle."""

    result = _copy_mapping(pointer, "active pointer")
    _require_exact_common(result, "active_evaluation_bundle")
    _require_exact_keys(
        result,
        {
            "schema_version",
            "artifact_type",
            "bundle_id",
            "seal_hash",
            "selection_revision",
            "selected_at",
            "actor",
            "reason",
        },
        "active pointer",
    )
    validate_safe_identifier(result["bundle_id"], field="bundle_id")
    _validate_sha256(result["seal_hash"], "seal_hash")
    if (
        not isinstance(result["selection_revision"], int)
        or isinstance(result["selection_revision"], bool)
        or result["selection_revision"] < 1
    ):
        raise EvaluationBundleError("selection_revision must be a positive integer")
    for field in ("selected_at", "actor", "reason"):
        _require_bounded_string(result[field], field, _MAX_EVIDENCE_TEXT)
    return result


def _validate_json_value(value: Any, *, path: str, seen: set[int]) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not (value == value and abs(value) != float("inf")):
            raise EvaluationBundleError(f"{path} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        object_id = id(value)
        if object_id in seen:
            raise EvaluationBundleError(f"{path} contains a reference cycle")
        seen.add(object_id)
        for key, child in value.items():
            if not isinstance(key, str):
                raise EvaluationBundleError(f"{path} contains a non-string object key")
            _validate_json_value(child, path=f"{path}.{key}", seen=seen)
        seen.remove(object_id)
        return
    if _is_sequence(value):
        object_id = id(value)
        if object_id in seen:
            raise EvaluationBundleError(f"{path} contains a reference cycle")
        seen.add(object_id)
        for index, child in enumerate(value):
            _validate_json_value(child, path=f"{path}[{index}]", seen=seen)
        seen.remove(object_id)
        return
    raise EvaluationBundleError(f"{path} contains unsupported type {type(value).__name__}")


def _copy_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationBundleError(f"{label} must be an object")
    canonical_json_bytes(value)
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def _validate_artifact_size(value: Mapping[str, Any], label: str, maximum: int) -> None:
    if len(artifact_json_bytes(value)) > maximum:
        raise EvaluationBundleError(f"{label} exceeds the {maximum}-byte JSON limit")


def _require_exact_common(value: Mapping[str, Any], artifact_type: str) -> None:
    _require_keys(value, {"schema_version", "artifact_type"}, artifact_type)
    if value["schema_version"] != SCHEMA_VERSION:
        raise EvaluationBundleError("unsupported schema_version")
    if artifact_type not in ARTIFACT_TYPES or value["artifact_type"] != artifact_type:
        raise EvaluationBundleError(f"artifact_type must be {artifact_type}")


def _require_keys(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    missing = sorted(fields - value.keys())
    if missing:
        raise EvaluationBundleError(f"{label} missing required fields: {', '.join(missing)}")


def _require_exact_keys(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    _require_keys(value, fields, label)
    unexpected = sorted(value.keys() - fields)
    if unexpected:
        raise EvaluationBundleError(
            f"{label} contains unexpected fields: {', '.join(unexpected)}"
        )


def _require_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationBundleError(f"{field} must be a non-empty string")
    return value


def _require_bounded_string(value: Any, field: str, maximum: int) -> str:
    result = _require_non_empty_string(value, field)
    if len(result) > maximum:
        raise EvaluationBundleError(f"{field} exceeds the {maximum}-character limit")
    return result


def _validate_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise EvaluationBundleError(f"{field} must be a lowercase SHA-256")
    return value


def _validate_positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EvaluationBundleError(f"{field} must be a positive integer")
    return value


def _validate_file_description(value: Any, label: str, seen: set[str]) -> None:
    entry = _copy_mapping(value, label)
    _require_exact_keys(entry, {"path", "role", "size", "sha256"}, label)
    path = validate_relative_path(entry["path"], field=f"{label}.path")
    if path in seen:
        raise EvaluationBundleError(f"duplicate file path: {path}")
    seen.add(path)
    _require_non_empty_string(entry["role"], f"{label}.role")
    if not isinstance(entry["size"], int) or isinstance(entry["size"], bool) or entry["size"] < 0:
        raise EvaluationBundleError(f"{label}.size must be a non-negative integer")
    _validate_sha256(entry["sha256"], f"{label}.sha256")


def _validate_turn_expectations(value: Mapping[str, Any]) -> None:
    expectations = _copy_mapping(value, "turn expectations")
    _require_exact_keys(
        expectations,
        {
            "retrieval_path",
            "filters",
            "source_ids",
            "status",
            "check_ids",
            "relational_required",
        },
        "turn expectations",
    )
    if not _is_sequence(expectations["retrieval_path"]) or not expectations["retrieval_path"]:
        raise EvaluationBundleError("expectations.retrieval_path must be a non-empty array")
    for item in expectations["retrieval_path"]:
        validate_safe_identifier(item, field="expectations.retrieval_path")
    if not _is_sequence(expectations["filters"]):
        raise EvaluationBundleError("expectations.filters must be an array")
    if len(expectations["filters"]) > _MAX_FILTERS_PER_TURN:
        raise EvaluationBundleError("expectations.filters exceeds its item limit")
    for index, raw_filter in enumerate(expectations["filters"]):
        item = _copy_mapping(raw_filter, f"expectations.filters[{index}]")
        _require_exact_keys(
            item,
            {"field", "operator", "value"},
            f"expectations.filters[{index}]",
        )
        validate_safe_identifier(item["field"], field="expectations.filter.field")
        validate_safe_identifier(item["operator"], field="expectations.filter.operator")
        _validate_structured_value(
            item["value"], "expectations.filter.value", maximum_string=256
        )
    for field in ("source_ids", "check_ids"):
        items = expectations[field]
        if not _is_sequence(items):
            raise EvaluationBundleError(f"expectations.{field} must be an array")
        seen_items: set[str] = set()
        for item in items:
            identifier = validate_safe_identifier(item, field=f"expectations.{field}")
            if identifier in seen_items:
                raise EvaluationBundleError(f"expectations.{field} contains duplicates")
            seen_items.add(identifier)
    if expectations["status"] not in {"supported", "not_found", "filtered"}:
        raise EvaluationBundleError("expectations.status is invalid")
    if not isinstance(expectations["relational_required"], bool):
        raise EvaluationBundleError("expectations.relational_required must be a boolean")


def _reject_keys_recursive(value: Any, forbidden: frozenset[str] | set[str], label: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = key.strip().lower().replace("-", "_")
            if normalized in forbidden:
                raise EvaluationBundleError(f"{label} contains forbidden field: {key}")
            _reject_keys_recursive(child, forbidden, label)
    elif _is_sequence(value):
        for child in value:
            _reject_keys_recursive(child, forbidden, label)


def _validate_trial_evidence(item: Mapping[str, Any], label: str) -> None:
    _require_exact_keys(
        item,
        {
            "case_id",
            "turn_id",
            "checks",
            "failed_checks",
            "retrieval",
            "relational",
            "semantic_review",
        },
        label,
    )
    validate_safe_identifier(item["case_id"], field=f"{label}.case_id")
    validate_safe_identifier(item["turn_id"], field=f"{label}.turn_id")
    if not _is_sequence(item["checks"]) or not _is_sequence(item["failed_checks"]):
        raise EvaluationBundleError(f"{label} checks and failed_checks must be arrays")
    if len(item["checks"]) > _MAX_CHECKS_PER_TURN:
        raise EvaluationBundleError(f"{label}.checks exceeds its item limit")
    if len(item["failed_checks"]) > _MAX_CHECKS_PER_TURN:
        raise EvaluationBundleError(f"{label}.failed_checks exceeds its item limit")
    check_ids: set[str] = set()
    for index, raw_check in enumerate(item["checks"]):
        check = _copy_mapping(raw_check, f"{label}.checks[{index}]")
        _require_exact_keys(
            check,
            {"check_id", "passed", "observed", "expected"},
            f"{label}.checks[{index}]",
        )
        check_id = validate_safe_identifier(check["check_id"], field="check_id")
        if check_id in check_ids:
            raise EvaluationBundleError(f"{label} contains a duplicate check_id")
        check_ids.add(check_id)
        if not isinstance(check["passed"], bool):
            raise EvaluationBundleError(f"{label} check passed must be a boolean")
        _validate_structured_value(check["observed"], f"{label}.checks[{index}].observed")
        _validate_structured_value(check["expected"], f"{label}.checks[{index}].expected")
    failed = item["failed_checks"]
    if any(not isinstance(value, str) or value not in check_ids for value in failed):
        raise EvaluationBundleError(f"{label}.failed_checks must name declared checks")
    if len(set(failed)) != len(failed):
        raise EvaluationBundleError(f"{label}.failed_checks contains duplicates")
    retrieval = _copy_mapping(item["retrieval"], f"{label}.retrieval")
    _require_exact_keys(
        retrieval,
        {"path", "filters", "sources", "expected_state_mismatch"},
        f"{label}.retrieval",
    )
    if not _is_sequence(retrieval["path"]) or not retrieval["path"]:
        raise EvaluationBundleError(f"{label}.retrieval.path must be a non-empty array")
    if len(retrieval["path"]) > 16:
        raise EvaluationBundleError(f"{label}.retrieval.path exceeds its item limit")
    for value in retrieval["path"]:
        validate_safe_identifier(value, field=f"{label}.retrieval.path")
    if not _is_sequence(retrieval["filters"]):
        raise EvaluationBundleError(f"{label}.retrieval.filters must be an array")
    if len(retrieval["filters"]) > _MAX_FILTERS_PER_TURN:
        raise EvaluationBundleError(f"{label}.retrieval.filters exceeds its item limit")
    for index, raw_filter in enumerate(retrieval["filters"]):
        filter_item = _copy_mapping(raw_filter, f"{label}.retrieval.filters[{index}]")
        _require_exact_keys(
            filter_item,
            {"field", "operator", "value"},
            f"{label}.retrieval.filters[{index}]",
        )
        validate_safe_identifier(filter_item["field"], field="filter.field")
        validate_safe_identifier(filter_item["operator"], field="filter.operator")
        _validate_structured_value(filter_item["value"], "filter.value", maximum_string=256)
    if not _is_sequence(retrieval["sources"]):
        raise EvaluationBundleError(f"{label}.retrieval.sources must be an array")
    if len(retrieval["sources"]) > _MAX_SOURCES_PER_TURN:
        raise EvaluationBundleError(f"{label}.retrieval.sources exceeds its item limit")
    for index, raw_source in enumerate(retrieval["sources"]):
        source = _copy_mapping(raw_source, f"{label}.retrieval.sources[{index}]")
        _require_exact_keys(
            source,
            {"source_id", "rank", "citation_rank", "expected_state_mismatch"},
            f"{label}.retrieval.sources[{index}]",
        )
        validate_safe_identifier(source["source_id"], field="source_id")
        _validate_positive_integer(source["rank"], "source.rank")
        if source["citation_rank"] is not None:
            _validate_positive_integer(source["citation_rank"], "source.citation_rank")
        if not isinstance(source["expected_state_mismatch"], bool):
            raise EvaluationBundleError("source.expected_state_mismatch must be a boolean")
    if not isinstance(retrieval["expected_state_mismatch"], bool):
        raise EvaluationBundleError("retrieval.expected_state_mismatch must be a boolean")
    relational = item["relational"]
    if relational is not None:
        relational = _copy_mapping(relational, f"{label}.relational")
        _require_exact_keys(
            relational,
            {"sql_intent", "normalized_result", "row_order_basis"},
            f"{label}.relational",
        )
        _require_bounded_string(
            relational["sql_intent"], "relational.sql_intent", _MAX_EVIDENCE_TEXT
        )
        _validate_structured_value(
            relational["normalized_result"],
            "relational.normalized_result",
            maximum_string=256,
        )
        _require_bounded_string(
            relational["row_order_basis"],
            "relational.row_order_basis",
            _MAX_SHORT_TEXT,
        )
    review = _copy_mapping(item["semantic_review"], f"{label}.semantic_review")
    _require_exact_keys(review, {"status", "facts"}, f"{label}.semantic_review")
    if review["status"] not in {"approved", "rejected", "not_evaluated"}:
        raise EvaluationBundleError("semantic_review.status is invalid")
    if not _is_sequence(review["facts"]):
        raise EvaluationBundleError("semantic_review.facts must be an array")
    if len(review["facts"]) > _MAX_FACTS_PER_TURN:
        raise EvaluationBundleError("semantic_review.facts exceeds its item limit")
    for index, raw_fact in enumerate(review["facts"]):
        fact = _copy_mapping(raw_fact, f"semantic_review.facts[{index}]")
        _require_exact_keys(fact, {"name", "value", "unit"}, "semantic fact")
        _require_bounded_string(fact["name"], "semantic fact.name", _MAX_SHORT_TEXT)
        _validate_structured_value(
            fact["value"], "semantic fact.value", maximum_string=256
        )
        if fact["unit"] is not None:
            _require_bounded_string(fact["unit"], "semantic fact.unit", 64)


def _validate_trial_usage(value: Any) -> None:
    usage = _copy_mapping(value, "usage")
    _require_exact_keys(usage, {"state", "by_turn", "total"}, "usage")
    if usage["state"] not in {"measured", "unmeasured"}:
        raise EvaluationBundleError("usage.state is invalid")
    if not _is_sequence(usage["by_turn"]):
        raise EvaluationBundleError("usage.by_turn must be an array")
    if len(usage["by_turn"]) > _MAX_TRIAL_PAIRS:
        raise EvaluationBundleError("usage.by_turn exceeds its item limit")
    for index, raw_item in enumerate(usage["by_turn"]):
        item = _copy_mapping(raw_item, f"usage.by_turn[{index}]")
        _require_exact_keys(
            item,
            {"case_id", "turn_id", "input_tokens", "output_tokens", "cost"},
            f"usage.by_turn[{index}]",
        )
        validate_safe_identifier(item["case_id"], field="usage.case_id")
        validate_safe_identifier(item["turn_id"], field="usage.turn_id")
        _validate_usage_numbers(item, f"usage.by_turn[{index}]")
    total = _copy_mapping(usage["total"], "usage.total")
    _require_exact_keys(total, {"input_tokens", "output_tokens", "cost"}, "usage.total")
    _validate_usage_numbers(total, "usage.total")
    if usage["state"] == "unmeasured" and (usage["by_turn"] or any(total[field] is not None for field in total)):
        raise EvaluationBundleError("unmeasured usage must not contain measurements")


def _validate_usage_numbers(value: Mapping[str, Any], label: str) -> None:
    for field in ("input_tokens", "output_tokens"):
        item = value[field]
        if item is not None and (
            not isinstance(item, int) or isinstance(item, bool) or item < 0
        ):
            raise EvaluationBundleError(f"{label}.{field} must be a non-negative integer or null")
    cost = value["cost"]
    if cost is not None and (
        not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0
    ):
        raise EvaluationBundleError(f"{label}.cost must be a non-negative number or null")


def _validate_process_exit_status(value: Any) -> None:
    status = _copy_mapping(value, "process_exit_status")
    _require_exact_keys(status, {"code", "signal"}, "process_exit_status")
    if status["code"] is not None and (
        not isinstance(status["code"], int) or isinstance(status["code"], bool)
    ):
        raise EvaluationBundleError("process_exit_status.code must be an integer or null")
    if status["signal"] is not None:
        validate_safe_identifier(status["signal"], field="process_exit_status.signal")


def _validate_structured_value(
    value: Any,
    label: str,
    *,
    maximum_string: int = _MAX_EVIDENCE_TEXT,
    depth: int = 0,
) -> None:
    if value is None or isinstance(value, (bool, int, float)):
        _validate_json_value(value, path=label, seen=set())
        return
    if isinstance(value, str):
        _require_bounded_string(value, label, maximum_string)
        return
    if _is_sequence(value):
        if depth >= 2:
            raise EvaluationBundleError(f"{label} exceeds the structured nesting limit")
        if len(value) > _MAX_STRUCTURED_ITEMS:
            raise EvaluationBundleError(f"{label} exceeds its item limit")
        for index, child in enumerate(value):
            _validate_structured_value(
                child,
                f"{label}[{index}]",
                maximum_string=maximum_string,
                depth=depth + 1,
            )
        return
    raise EvaluationBundleError(f"{label} must contain only structured scalar values")


def _validate_no_sensitive_strings(value: Any, label: str) -> None:
    if isinstance(value, str):
        if contains_sensitive_identifier_pattern(value):
            raise EvaluationBundleError(f"{label} contains sensitive or machine-local text")
    elif isinstance(value, Mapping):
        for child in value.values():
            _validate_no_sensitive_strings(child, label)
    elif _is_sequence(value):
        for child in value:
            _validate_no_sensitive_strings(child, label)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
