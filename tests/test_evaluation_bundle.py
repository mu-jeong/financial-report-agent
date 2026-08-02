from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.core.evaluation_bundle import (
    EvaluationBundleError,
    artifact_json_bytes,
    artifact_json_sha256,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    normalize_evaluation_dataset,
    resolve_safe_relative_path,
    seal_hash,
    validate_capture_receipt,
    validate_active_pointer,
    validate_approval,
    validate_manifest,
    validate_relative_path,
    validate_seal,
    validate_snapshot_reference,
    validate_trial_run,
    validate_trial_run_receipt,
    validate_validation_result,
)
from src.core.artifact_io import atomic_write_json


HASH = "a" * 64


def expectations() -> dict:
    return {
        "retrieval_path": ["vector"],
        "filters": [],
        "source_ids": ["source-1"],
        "status": "supported",
        "check_ids": ["required-check"],
        "relational_required": False,
    }


def dataset(*, cases: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "evaluation_dataset",
        "dataset_id": "dataset-1",
        "data_revision": "revision-1",
        "cases": cases
        or [
            {
                "case_id": "case-1",
                "turn": {
                    "turn_id": "turn-1",
                    "question": "What changed?",
                    "expectations": expectations(),
                },
            }
        ],
    }


def trial() -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "trial_run",
        "bundle_id": "bundle-1",
        "content_hash": HASH,
        "validation_hash": HASH,
        "case_turns": [{"case_id": "case-1", "turn_id": "turn-1"}],
        "evidence": [
            {
                "case_id": "case-1",
                "turn_id": "turn-1",
                "checks": [
                    {
                        "check_id": "revenue-check",
                        "passed": True,
                        "observed": 3,
                        "expected": 3,
                    }
                ],
                "failed_checks": [],
                "retrieval": {
                    "path": ["vector"],
                    "filters": [{"field": "company", "operator": "equals", "value": "ACME"}],
                    "sources": [
                        {
                            "source_id": "source-1",
                            "rank": 1,
                            "citation_rank": 1,
                            "expected_state_mismatch": False,
                        }
                    ],
                    "expected_state_mismatch": False,
                },
                "relational": None,
                "semantic_review": {
                    "status": "approved",
                    "facts": [{"name": "revenue", "value": 3, "unit": "USD"}],
                },
            }
        ],
        "usage": {
            "state": "unmeasured",
            "by_turn": [],
            "total": {"input_tokens": None, "output_tokens": None, "cost": None},
        },
        "completed_case_count": 1,
        "completed_turn_count": 1,
        "expected_case_count": 1,
        "expected_turn_count": 1,
        "started_at": "2026-08-01T00:00:00Z",
        "finished_at": "2026-08-01T00:01:00Z",
        "process_exit_status": {"code": 0, "signal": None},
        "status": "passed",
        "reproduction": {"complete": True, "fingerprint": HASH},
        "reproduction_hash": HASH,
        "models": {"embedding": "embed-v1", "generation": "gen-v1", "reranking": None},
    }


def manifest() -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "evaluation_bundle_manifest",
        "bundle_id": "bundle-1",
        "created_at": "2026-08-01T00:00:00Z",
        "actor": "operator",
        "dataset": {
            "path": "evaluation_dataset.json",
            "version": "1",
            "case_count": 1,
            "turn_count": 1,
            "sha256": HASH,
        },
        "source_snapshot": {
            "retrieval_id": "retrieval-1",
            "snapshot_id": "snapshot-1",
            "build_id": "build-1",
            "publication_generation": 1,
            "write_generation": 1,
        },
        "revisions": {"data_revision": "data-1", "index_revision": "index-1"},
        "embedding_profile": {
            "profile_id": "profile-1",
            "sha256": HASH,
            "components": {
                "model": "embed-v1",
                "dimension": 1024,
                "distance_metric": "cosine",
                "normalization": "l2",
                "text_format": "title-body",
                "document_extraction": "pdf-v1",
                "parent_chunking": "parent-v1",
                "child_chunking": "child-v1",
            },
        },
        "statistics": {
            "document_count": 1,
            "parent_chunk_count": 2,
            "child_chunk_count": 3,
            "vector_count": 3,
            "data_date_start": "2026-01-01",
            "data_date_end": "2026-06-30",
        },
        "base_files": [
            {"path": "evaluation_dataset.json", "role": "evaluation_dataset", "size": 1, "sha256": HASH},
            {"path": "capture_receipt.json", "role": "capture_receipt", "size": 1, "sha256": HASH},
            {"path": "snapshot_reference.json", "role": "snapshot_reference", "size": 1, "sha256": HASH},
            {"path": "data/reports.db", "role": "reports_database", "size": 1, "sha256": HASH},
            {"path": "data/retrieval/v2/catalog.sqlite3", "role": "catalog_database", "size": 1, "sha256": HASH},
        ],
        "snapshot_reference_hash": HASH,
        "dataset_set_hash": HASH,
        "execution_policy": {
            "data_and_index_immutable": True,
            "model_values_recorded_at_runtime": True,
        },
        "storage_policy": {"single_machine_only": True, "automatic_backup": False},
    }


def approval() -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "approval",
        "bundle_id": "bundle-1",
        "actor": "operator",
        "reason": "structured checks passed",
        "approved_at": "2026-08-01T00:02:00Z",
        "content_hash": HASH,
        "validation_hash": HASH,
        "trial_run_hash": HASH,
        "trial_run_receipt_hash": HASH,
    }


def seal() -> dict:
    return {
        "schema_version": 1,
        "artifact_type": "seal",
        "bundle_id": "bundle-1",
        "manifest_hash": HASH,
        "dataset_set_hash": HASH,
        "snapshot_reference_hash": HASH,
        "validation_hash": HASH,
        "trial_run_hash": HASH,
        "trial_run_receipt_hash": HASH,
        "approval_hash": HASH,
        "sealed_at": "2026-08-01T00:02:00Z",
    }


def test_canonical_hash_is_order_independent_and_matches_compact_utf8_json() -> None:
    first = {"z": 1, "한글": [True, None], "a": {"b": 2}}
    second = {"a": {"b": 2}, "한글": [True, None], "z": 1}
    expected = json.dumps(
        first, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert canonical_json_bytes(first) == expected
    assert canonical_sha256(first) == canonical_sha256(second)
    assert canonical_sha256(first) == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize("value", [{"x": float("nan")}, {1: "not a JSON object key"}])
def test_canonical_hash_rejects_non_json_material(value: object) -> None:
    with pytest.raises(EvaluationBundleError):
        canonical_sha256(value)


def test_single_turn_convenience_form_normalizes_to_unified_turns() -> None:
    normalized = normalize_evaluation_dataset(dataset())

    assert "turn" not in normalized["cases"][0]
    assert normalized["cases"][0]["turns"] == [dataset()["cases"][0]["turn"]]


def test_multiturn_order_is_preserved() -> None:
    turns = [
        {"turn_id": "t1", "question": "First", "expectations": expectations()},
        {"turn_id": "t2", "question": "Second", "expectations": expectations()},
        {"turn_id": "t3", "question": "Third", "expectations": expectations()},
    ]
    value = dataset(cases=[{"case_id": "case-1", "turns": turns}])

    normalized = normalize_evaluation_dataset(value)

    assert [item["turn_id"] for item in normalized["cases"][0]["turns"]] == ["t1", "t2", "t3"]


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value.pop("dataset_id"), "dataset_id"),
        (lambda value: value["cases"][0]["turn"].pop("expectations"), "expectations"),
        (lambda value: value.update(cases=[]), "non-empty"),
        (
            lambda value: value["cases"].append(deepcopy(value["cases"][0])),
            "duplicate case_id",
        ),
    ],
)
def test_dataset_required_fields_and_uniqueness(mutator, message: str) -> None:
    value = dataset()
    mutator(value)

    with pytest.raises(EvaluationBundleError, match=message):
        normalize_evaluation_dataset(value)


def test_duplicate_turn_id_is_rejected_within_a_case() -> None:
    turn = {"turn_id": "turn-1", "question": "Q", "expectations": expectations()}
    value = dataset(cases=[{"case_id": "case-1", "turns": [turn, deepcopy(turn)]}])

    with pytest.raises(EvaluationBundleError, match="duplicate turn_id"):
        normalize_evaluation_dataset(value)


@pytest.mark.parametrize(
    "location",
    ["case", "turn", "expectations"],
)
def test_dataset_rejects_unknown_nested_fields(location: str) -> None:
    value = dataset()
    target = value["cases"][0]
    if location == "turn":
        target = target["turn"]
    elif location == "expectations":
        target = target["turn"]["expectations"]
    target["extra"] = "not allowed"

    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        normalize_evaluation_dataset(value)


@pytest.mark.parametrize(
    "path",
    ["../escape.json", "nested/../../escape", "/absolute/file", r"C:\temp\file", "a//b"],
)
def test_relative_paths_reject_traversal_and_non_normalized_forms(path: str) -> None:
    with pytest.raises(EvaluationBundleError):
        validate_relative_path(path)


def test_resolved_path_rejects_symlink_components(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable for this test account")

    with pytest.raises(EvaluationBundleError, match="symbolic link"):
        resolve_safe_relative_path(tmp_path, "link/file.json")


@pytest.mark.parametrize(
    "forbidden_key",
    ["raw_pdf_text", "provider_response", "full_generated_answer", "raw_completion", "chunk_text"],
)
def test_trial_evidence_rejects_raw_or_sensitive_payloads(forbidden_key: str) -> None:
    value = trial()
    value["evidence"][0][forbidden_key] = "must not be persisted"

    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_trial_run(value)


def test_trial_evidence_allows_only_structured_answer_facts_not_full_answer() -> None:
    assert validate_trial_run(trial())["status"] == "passed"
    value = trial()
    value["evidence"][0]["answer"] = "the complete generated answer"

    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_trial_run(value)


@pytest.mark.parametrize(
    "sensitive_value",
    ["user@example.com", "api_key=secret-value", r"C:\Users\operator\evidence.json"],
)
def test_trial_rejects_sensitive_values_even_under_allowed_fields(sensitive_value: str) -> None:
    value = trial()
    value["evidence"][0]["semantic_review"]["facts"][0]["value"] = sensitive_value

    with pytest.raises(EvaluationBundleError, match="sensitive or machine-local"):
        validate_trial_run(value)


def test_trial_rejects_large_body_hidden_in_an_allowed_observed_value() -> None:
    value = trial()
    value["evidence"][0]["checks"][0]["observed"] = "x" * 100_000

    with pytest.raises(EvaluationBundleError, match="character limit"):
        validate_trial_run(value)


def test_trial_rejects_aggregate_json_over_two_megabytes() -> None:
    value = trial()
    value["evidence"][0]["checks"] = [
        {
            "check_id": f"check-{index}",
            "passed": True,
            "observed": "x" * 500,
            "expected": "x" * 500,
        }
        for index in range(2_100)
    ]

    with pytest.raises(EvaluationBundleError, match="2097152-byte JSON limit"):
        validate_trial_run(value)


def test_passed_trial_requires_evidence_complete_counts_and_declared_coverage() -> None:
    value = trial()
    value["evidence"] = []
    value["completed_case_count"] = 0
    value["completed_turn_count"] = 0
    value["expected_case_count"] = 0
    value["expected_turn_count"] = 0

    with pytest.raises(EvaluationBundleError, match="cover every declared case/turn"):
        validate_trial_run(value)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda value: value["evidence"][0]["checks"][0].update(passed=False),
            "every required check",
        ),
        (
            lambda value: value["reproduction"].update(complete=False),
            "complete reproduction",
        ),
        (
            lambda value: value["process_exit_status"].update(code=1),
            "successful process exit",
        ),
    ],
)
def test_passed_trial_requires_checks_reproduction_and_successful_exit(mutator, message: str) -> None:
    value = trial()
    mutator(value)

    with pytest.raises(EvaluationBundleError, match=message):
        validate_trial_run(value)


def test_seal_forbids_active_pointer_history() -> None:
    value = seal()
    value["previous_active_bundle_id"] = "bundle-old"

    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_seal(value)


def test_active_pointer_changes_do_not_change_seal_hash() -> None:
    immutable_seal = seal()
    first_pointer = {
        "schema_version": 1,
        "artifact_type": "active_evaluation_bundle",
        "bundle_id": "bundle-1",
        "seal_hash": seal_hash(immutable_seal),
        "selection_revision": 1,
        "selected_at": "2026-08-01T00:03:00Z",
        "actor": "operator",
        "reason": "initial selection",
    }
    second_pointer = {
        **first_pointer,
        "selection_revision": 2,
        "selected_at": "2026-08-02T00:03:00Z",
        "reason": "selected again",
    }

    validate_active_pointer(first_pointer)
    validate_active_pointer(second_pointer)

    assert first_pointer != second_pointer
    assert seal_hash(immutable_seal) == first_pointer["seal_hash"] == second_pointer["seal_hash"]


def test_seal_hash_matches_exact_atomic_json_file_bytes(tmp_path: Path) -> None:
    immutable_seal = seal()
    target = tmp_path / "seal.json"

    atomic_write_json(target, immutable_seal)

    assert target.read_bytes() == artifact_json_bytes(immutable_seal)
    assert seal_hash(immutable_seal) == artifact_json_sha256(immutable_seal)
    assert seal_hash(immutable_seal) == file_sha256(target)
    assert seal_hash(immutable_seal) != canonical_sha256(immutable_seal)


def test_public_contract_uses_bundle_terminology() -> None:
    from src.core import evaluation_bundle

    public_names = [name for name in dir(evaluation_bundle) if not name.startswith("_")]
    assert all("baseline" not in name.lower() for name in public_names)


def test_manifest_accepts_only_complete_foundation_contract() -> None:
    assert validate_manifest(manifest())["bundle_id"] == "bundle-1"


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value["source_snapshot"].update(extra="no"), "unexpected fields"),
        (lambda value: value["embedding_profile"]["components"].update(model=""), "non-empty"),
        (
            lambda value: value["base_files"].append(
                {"path": "seal.json", "role": "seal", "size": 1, "sha256": HASH}
            ),
            "role is not allowed",
        ),
        (
            lambda value: value["base_files"][0].update(path="trial_run.json"),
            "path does not match",
        ),
        (lambda value: value["storage_policy"].update(automatic_backup=True), "local-only"),
    ],
)
def test_manifest_rejects_extra_incomplete_or_post_manifest_material(mutator, message: str) -> None:
    value = manifest()
    mutator(value)

    with pytest.raises(EvaluationBundleError, match=message):
        validate_manifest(value)


def test_approval_requires_exact_hash_links_and_nonempty_reason() -> None:
    assert validate_approval(approval())["approved_at"].endswith("Z")
    invalid = approval()
    invalid["reason"] = ""
    with pytest.raises(EvaluationBundleError, match="reason must be a non-empty"):
        validate_approval(invalid)
    invalid = approval()
    invalid["trial_run_hash"] = "not-a-hash"
    with pytest.raises(EvaluationBundleError, match="lowercase SHA-256"):
        validate_approval(invalid)
    invalid = approval()
    invalid["previous_active_bundle_id"] = "bundle-old"
    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_approval(invalid)


def test_capture_receipt_requires_verified_pin_when_complete() -> None:
    receipt = {
        "schema_version": 1,
        "artifact_type": "capture_receipt",
        "bundle_id": "bundle-1",
        "source_snapshot": {
            "snapshot_id": "snapshot-1",
            "build_id": "build-1",
            "publication_generation": 1,
        },
        "started_at": "2026-08-01T00:00:00Z",
        "finished_at": "2026-08-01T00:01:00Z",
        "copied_files": [
            {"path": "data/reports.db", "role": "reports", "size": 12, "sha256": HASH}
        ],
        "referenced_files": [
            {"path": "retrieval/v2/snapshot.faiss", "role": "vectors", "size": 20, "sha256": HASH}
        ],
        "temporary_pin_id": "job-1",
        "reference_set_id": "references-1",
        "pin_verified": True,
        "complete": True,
    }

    assert validate_capture_receipt(receipt)["complete"] is True
    receipt["pin_verified"] = False
    with pytest.raises(EvaluationBundleError, match="verified pin"):
        validate_capture_receipt(receipt)


def test_complete_capture_requires_referenced_v2_files_and_exact_source_identity() -> None:
    receipt = {
        "schema_version": 1,
        "artifact_type": "capture_receipt",
        "bundle_id": "bundle-1",
        "source_snapshot": {
            "snapshot_id": "snapshot-1",
            "build_id": "build-1",
            "publication_generation": 1,
        },
        "started_at": "2026-08-01T00:00:00Z",
        "finished_at": "2026-08-01T00:01:00Z",
        "copied_files": [
            {"path": "data/reports.db", "role": "reports", "size": 12, "sha256": HASH}
        ],
        "referenced_files": [],
        "temporary_pin_id": "job-1",
        "reference_set_id": "references-1",
        "pin_verified": True,
        "complete": True,
    }

    with pytest.raises(EvaluationBundleError, match="requires referenced_files"):
        validate_capture_receipt(receipt)
    receipt["source_snapshot"]["source_root_path"] = r"C:\private\data"
    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_capture_receipt(receipt)


def test_snapshot_reference_paths_must_be_described_and_pin_names_are_forbidden() -> None:
    reference = {
        "schema_version": 1,
        "artifact_type": "snapshot_reference",
        "source_root_id": "source-1",
        "snapshot_id": "snapshot-1",
        "build_id": "build-1",
        "publication_generation": 1,
        "files": [
            {"path": "retrieval/v2/snapshot.faiss", "role": "vectors", "size": 20, "sha256": HASH},
            {"path": "retrieval/v2/check.sqlite3", "role": "checkpoint", "size": 10, "sha256": HASH},
        ],
        "publication_validation_files": [],
        "checkpoint_files": ["retrieval/v2/check.sqlite3"],
        "reference_set_id": "references-1",
        "reference_set_hash": HASH,
    }

    assert validate_snapshot_reference(reference)["reference_set_hash"] == HASH
    reference["checkpoint_files"] = ["retrieval/v2/missing.sqlite3"]
    with pytest.raises(EvaluationBundleError, match="not present"):
        validate_snapshot_reference(reference)
    reference["checkpoint_files"] = []
    reference["pin_path"] = "evaluation_pins/staging-job-1.json"
    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_snapshot_reference(reference)


@pytest.mark.parametrize("field", ["source_root_path", "pin_path"])
def test_snapshot_reference_rejects_absolute_root_and_pin_path_fields(field: str) -> None:
    reference = {
        "schema_version": 1,
        "artifact_type": "snapshot_reference",
        "source_root_id": "source-1",
        "snapshot_id": "snapshot-1",
        "build_id": "build-1",
        "publication_generation": 1,
        "files": [
            {"path": "retrieval/v2/snapshot.faiss", "role": "vectors", "size": 20, "sha256": HASH}
        ],
        "publication_validation_files": [],
        "checkpoint_files": [],
        "reference_set_id": "references-1",
        "reference_set_hash": HASH,
        field: r"C:\private\data",
    }

    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_snapshot_reference(reference)


def test_validation_result_links_hashes_counts_and_reference_set() -> None:
    validation = {
        "schema_version": 1,
        "artifact_type": "validation",
        "bundle_id": "bundle-1",
        "manifest_hash": HASH,
        "dataset_set_hash": HASH,
        "snapshot_reference_hash": HASH,
        "reference_set_id": "references-1",
        "expected_case_count": 2,
        "expected_turn_count": 3,
        "validated_case_count": 2,
        "validated_turn_count": 3,
        "checks": [{"check_id": "hashes", "passed": True}],
        "issues": [],
        "status": "passed",
        "validated_at": "2026-08-01T00:02:00Z",
    }

    assert validate_validation_result(validation)["status"] == "passed"
    validation["validated_turn_count"] = 2
    with pytest.raises(EvaluationBundleError, match="complete and issue-free"):
        validate_validation_result(validation)


def test_validation_result_rejects_unknown_nested_check_fields() -> None:
    validation = {
        "schema_version": 1,
        "artifact_type": "validation",
        "bundle_id": "bundle-1",
        "manifest_hash": HASH,
        "dataset_set_hash": HASH,
        "snapshot_reference_hash": HASH,
        "reference_set_id": "references-1",
        "expected_case_count": 1,
        "expected_turn_count": 1,
        "validated_case_count": 1,
        "validated_turn_count": 1,
        "checks": [{"check_id": "hashes", "passed": True, "extra": "no"}],
        "issues": [],
        "status": "passed",
        "validated_at": "2026-08-01T00:02:00Z",
    }

    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_validation_result(validation)


def test_trial_run_receipt_binds_trial_hash_and_complete_counts() -> None:
    receipt = {
        "schema_version": 1,
        "artifact_type": "trial_run_receipt",
        "bundle_id": "bundle-1",
        "content_hash": HASH,
        "validation_hash": HASH,
        "trial_run_hash": HASH,
        "expected_case_count": 1,
        "expected_turn_count": 2,
        "completed_case_count": 1,
        "completed_turn_count": 2,
        "started_at": "2026-08-01T00:03:00Z",
        "finished_at": "2026-08-01T00:04:00Z",
        "process_exit_status": {"code": 0, "signal": None},
        "status": "passed",
        "complete": True,
    }

    assert validate_trial_run_receipt(receipt)["trial_run_hash"] == HASH
    receipt["completed_turn_count"] = 1
    with pytest.raises(EvaluationBundleError, match="positive complete counts"):
        validate_trial_run_receipt(receipt)


def test_passed_trial_run_receipt_rejects_nonzero_process_exit() -> None:
    receipt = {
        "schema_version": 1,
        "artifact_type": "trial_run_receipt",
        "bundle_id": "bundle-1",
        "content_hash": HASH,
        "validation_hash": HASH,
        "trial_run_hash": HASH,
        "expected_case_count": 1,
        "expected_turn_count": 1,
        "completed_case_count": 1,
        "completed_turn_count": 1,
        "started_at": "2026-08-01T00:03:00Z",
        "finished_at": "2026-08-01T00:04:00Z",
        "process_exit_status": {"code": 1, "signal": None},
        "status": "passed",
        "complete": True,
    }

    with pytest.raises(EvaluationBundleError, match="successful exit"):
        validate_trial_run_receipt(receipt)


def test_passed_trial_run_receipt_rejects_zero_workload_counts() -> None:
    receipt = {
        "schema_version": 1,
        "artifact_type": "trial_run_receipt",
        "bundle_id": "bundle-1",
        "content_hash": HASH,
        "validation_hash": HASH,
        "trial_run_hash": HASH,
        "expected_case_count": 0,
        "expected_turn_count": 0,
        "completed_case_count": 0,
        "completed_turn_count": 0,
        "started_at": "2026-08-01T00:03:00Z",
        "finished_at": "2026-08-01T00:04:00Z",
        "process_exit_status": {"code": 0, "signal": None},
        "status": "passed",
        "complete": True,
    }

    with pytest.raises(EvaluationBundleError, match="positive complete counts"):
        validate_trial_run_receipt(receipt)


def test_trial_run_receipt_rejects_raw_provider_content_recursively() -> None:
    receipt = {
        "schema_version": 1,
        "artifact_type": "trial_run_receipt",
        "bundle_id": "bundle-1",
        "content_hash": HASH,
        "validation_hash": HASH,
        "trial_run_hash": HASH,
        "expected_case_count": 0,
        "expected_turn_count": 0,
        "completed_case_count": 0,
        "completed_turn_count": 0,
        "started_at": "2026-08-01T00:03:00Z",
        "finished_at": "2026-08-01T00:04:00Z",
        "process_exit_status": {"code": None, "signal": None, "raw_completion": "raw"},
        "status": "execution_unavailable",
        "complete": False,
    }

    with pytest.raises(EvaluationBundleError, match="unexpected fields"):
        validate_trial_run_receipt(receipt)


@pytest.mark.parametrize("invalid_generation", [True, False, 0, -1, "1"])
def test_capture_receipt_requires_positive_integer_publication_generation(
    invalid_generation: object,
) -> None:
    receipt = {
        "schema_version": 1,
        "artifact_type": "capture_receipt",
        "bundle_id": "bundle-1",
        "source_snapshot": {
            "snapshot_id": "snapshot-1",
            "build_id": "build-1",
            "publication_generation": invalid_generation,
        },
        "started_at": "2026-08-01T00:00:00Z",
        "finished_at": "2026-08-01T00:01:00Z",
        "copied_files": [
            {"path": "data/reports.db", "role": "reports", "size": 12, "sha256": HASH}
        ],
        "referenced_files": [],
        "temporary_pin_id": "job-1",
        "reference_set_id": "references-1",
        "pin_verified": True,
        "complete": True,
    }

    with pytest.raises(EvaluationBundleError, match="positive integer"):
        validate_capture_receipt(receipt)


@pytest.mark.parametrize("invalid_generation", [True, False, 0, -1, "1"])
def test_snapshot_reference_requires_positive_integer_publication_generation(
    invalid_generation: object,
) -> None:
    reference = {
        "schema_version": 1,
        "artifact_type": "snapshot_reference",
        "source_root_id": "source-1",
        "snapshot_id": "snapshot-1",
        "build_id": "build-1",
        "publication_generation": invalid_generation,
        "files": [
            {"path": "retrieval/v2/snapshot.faiss", "role": "vectors", "size": 20, "sha256": HASH}
        ],
        "publication_validation_files": [],
        "checkpoint_files": [],
        "reference_set_id": "references-1",
        "reference_set_hash": HASH,
    }

    with pytest.raises(EvaluationBundleError, match="positive integer"):
        validate_snapshot_reference(reference)
