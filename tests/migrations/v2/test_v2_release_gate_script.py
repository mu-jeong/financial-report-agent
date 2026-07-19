from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.migrations.v2 import assemble_v2_release_gate as release_gate
from scripts.migrations.v2 import run_release_pytest
from src.migrations.v2.validation.installed_validation import (
    REQUIRED_DISTRIBUTIONS,
)
from tests.migrations.v2.test_v2_installed_validation_script import (
    _transition_evidence,
)


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_readonly(path: Path) -> Path:
    os.chmod(path, stat.S_IREAD)
    return path


def _junit_xml(*, declared_tests: int = 2, cases: int = 2) -> str:
    testcase_xml = "".join(
        f'<testcase classname="tests.test_release" name="test_{index}" time="0.001" />'
        for index in range(cases)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        f'<testsuite name="pytest" tests="{declared_tests}" failures="0" '
        f'errors="0" skipped="0" time="0.002">{testcase_xml}'
        "</testsuite></testsuites>"
    )


def _approval(role: str, bundle_sha256: str) -> dict:
    return {
        "schema_version": 1,
        "kind": "v2_release_review_approval",
        "reviewer_role": role,
        "reviewed_at_utc": "2026-07-21T00:00:00Z",
        "verdict": "approve",
        "approved": True,
        "release_eligible": False,
        "release_bundle_sha256": bundle_sha256,
        "open_p0_count": 0,
        "open_p1_count": 0,
    }


def _reader_parity_payload() -> dict:
    mismatch_counts = {
        field: 0 for field in release_gate.READER_PARITY_MISMATCH_FIELDS
    }
    workloads = {
        name: {
            "legacy_eligible_count": 1,
            "legacy_exact_score_tie_groups": 0,
            "mismatches": dict(mismatch_counts),
            "native_eligible_count": 1,
            "native_exact_score_tie_groups": 0,
            "passed": True,
            "request_count": 2,
        }
        for name in release_gate.READER_PARITY_WORKLOADS
    }
    return {
        "schema_version": 1,
        "kind": "v1_v2_copied_install_reader_parity",
        "status": "passed",
        "inputs": {
            "query_input_sha256": "1" * 64,
            "scope_input_sha256": "2" * 64,
        },
        "runtime": {
            "snapshot_id": "a" * 64,
            "snapshot_sha256": "b" * 64,
            "publication_generation": 1,
            "write_epoch": 0,
            "dimension": 2,
            "metric": "l2",
            "ntotal": 8,
            "conversion_manifest_sha256": "c" * 64,
            "legacy_mapping_sha256": "d" * 64,
            "source_manifest_sha256": "e" * 64,
        },
        "protocol": {
            "query_count": 2,
            "workload_count": 8,
            "request_count": 16,
            "k": 8,
            "exact_score_tie_policy": "score_group_then_chunk_uid_before_top_k",
            "l2_order": "ascending_score_then_chunk_uid_for_exact_ties",
            "inner_product_order": "descending_score_then_chunk_uid_for_exact_ties",
        },
        "counts": {
            "legacy_eligible_count": 8,
            "legacy_exact_score_tie_groups": 0,
            "native_eligible_count": 8,
            "native_exact_score_tie_groups": 0,
        },
        "mismatches": mismatch_counts,
        "workloads": workloads,
    }


def _compatibility_payload() -> dict:
    multiplicity_uid = "1" * 64
    parent_order_uid = "2" * 64
    new_uid = "3" * 64
    return {
        "schema_version": 1,
        "kind": "v2_successor_compatibility_exception_evidence",
        "passed": True,
        "approved": False,
        "release_eligible": False,
        "compatibility_exception_required": True,
        "runtime": {
            "active_snapshot_id": "c" * 64,
            "predecessor_snapshot_id": "a" * 64,
            "publication_generation": 2,
            "write_epoch": 1,
        },
        "snapshot_descriptors": {
            "active": {
                "snapshot_id": "c" * 64,
                "sha256": "d" * 64,
                "size_bytes": 40,
                "dimension": 2,
                "metric": "l2",
                "ntotal": 4,
            },
            "predecessor": {
                "snapshot_id": "a" * 64,
                "sha256": "b" * 64,
                "size_bytes": 50,
                "dimension": 2,
                "metric": "l2",
                "ntotal": 5,
            },
        },
        "structural_delta": {
            "active_report_count": 3,
            "predecessor_report_count": 2,
            "common_report_count": 2,
            "new_report_count": 1,
            "new_report_uids": [new_uid],
            "missing_report_count": 0,
            "missing_report_uids": [],
            "unique_chunk_content_loss": 0,
            "removed_duplicate_occurrences": 1,
            "multiplicity_delta_report_count": 1,
            "multiplicity_delta_reports": [
                {
                    "report_uid": multiplicity_uid,
                    "canonical_relative_path": "downloaded/multiplicity.pdf",
                    "predecessor_parent_count": 2,
                    "active_parent_count": 1,
                    "predecessor_chunk_count": 3,
                    "active_chunk_count": 2,
                }
            ],
            "parent_order_delta_report_count": 1,
            "parent_order_delta_reports": [
                {
                    "report_uid": parent_order_uid,
                    "canonical_relative_path": "downloaded/order.pdf",
                    "predecessor_parent_count": 1,
                    "active_parent_count": 1,
                    "predecessor_chunk_count": 1,
                    "active_chunk_count": 1,
                }
            ],
        },
        "retrieval_quality": {
            "k": 2,
            "predecessor_vector_queries": 5,
            "expected_content_rank_one": 5,
            "expected_content_within_k": 5,
            "source_top_one_equal": 5,
            "citation_complete_top_one": 5,
            "minimum_source_set_recall_at_k": 0.5,
            "mean_source_set_recall_at_k": 0.9,
            "gate_d_query_passed": True,
            "gate_d_query_spec_sha256": "e" * 64,
            "gate_d_expected_report_uid": new_uid,
        },
        "proposed_exception": {
            "reason_codes": [
                "collapse_exact_v1_duplicate_occurrences",
                "canonicalize_parent_order_from_source_rebuild",
            ],
            "unique_content_loss_allowed": 0,
            "citation_loss_allowed": 0,
            "approval_required": True,
        },
    }


def _compatibility_context() -> dict:
    return {
        "conversion": {
            "validation": {
                "snapshot_id": "a" * 64,
                "snapshot_sha256": "b" * 64,
                "report_count": 2,
                "chunk_count": 5,
            }
        },
        "successor_race": {
            "validation": {
                "active_snapshot_id": "c" * 64,
                "predecessor_snapshot_id": "a" * 64,
                "candidate_report_count": 3,
                "candidate_chunk_count": 4,
                "publication_generation": 2,
                "write_epoch": 1,
            }
        },
    }


def _compatibility_approval(compatibility_sha256: str) -> dict:
    payload = _compatibility_payload()
    structural = payload["structural_delta"]
    quality = payload["retrieval_quality"]
    return {
        "schema_version": 1,
        "kind": "v2_successor_compatibility_architect_approval",
        "reviewed_at_utc": "2026-07-18T04:01:11Z",
        "reviewer_role": "independent_architect",
        "verdict": "approve",
        "approved": True,
        "release_eligible": False,
        "open_p0_count": 0,
        "open_p1_count": 0,
        "source_evidence": {
            "relative_path": "compatibility.json",
            "sha256": compatibility_sha256,
            "active_snapshot_id": payload["runtime"]["active_snapshot_id"],
            "predecessor_snapshot_id": payload["runtime"]["predecessor_snapshot_id"],
        },
        "approved_exceptions": payload["proposed_exception"]["reason_codes"],
        "multiplicity_delta_report_uids": [
            item["report_uid"]
            for item in structural["multiplicity_delta_reports"]
        ],
        "parent_order_delta_report_uids": [
            item["report_uid"]
            for item in structural["parent_order_delta_reports"]
        ],
        "required_invariants": {
            "unique_chunk_content_loss": 0,
            "citation_loss": 0,
            "predecessor_vector_queries": quality["predecessor_vector_queries"],
            "expected_content_rank_one": quality["expected_content_rank_one"],
            "source_top_one_equal": quality["source_top_one_equal"],
            "citation_complete_top_one": quality["citation_complete_top_one"],
            "gate_d_query_passed": True,
        },
        "directives": [
            "Approval is limited to the named report UIDs and this exact snapshot pair.",
            "Any wider delta requires fresh review.",
            "This approval does not waive installed validation.",
        ],
    }


def _pytest_attestation(junit: Path) -> dict:
    executable = Path(sys.executable).resolve()
    nodeids = [
        "tests/test_release.py::test_0",
        "tests/test_release.py::test_1",
    ]
    temporary = junit.parent / f".{junit.name}.0123456789abcdef.tmp"
    return {
        "schema_version": 1,
        "kind": "v2_release_pytest_attestation",
        "status": "passed",
        "protocol": {
            "collection_exit_code": 0,
            "execution_exit_code": 0,
            "selection_args_allowed": False,
            "test_target": "tests",
        },
        "commands": {
            "working_directory": str(release_gate.REPOSITORY_ROOT.resolve()),
            "collection_argv": [
                str(executable),
                *run_release_pytest._COLLECTION_ARGUMENTS,
            ],
            "execution_argv": [
                str(executable),
                *run_release_pytest._EXECUTION_ARGUMENTS,
                f"--junitxml={temporary}",
            ],
            "environment": {
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
            },
        },
        "interpreter": {
            "executable": str(executable),
            "executable_sha256": release_gate._sha256_file(executable),
            "implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "pytest_version": importlib.metadata.version("pytest"),
        },
        "layouts": run_release_pytest._capture_release_layouts(
            release_gate.REPOSITORY_ROOT.resolve()
        ),
        "collection": {
            "count": len(nodeids),
            "nodeids": nodeids,
            "nodeids_sha256": hashlib.sha256(
                release_gate.canonical_json(nodeids).encode("utf-8")
            ).hexdigest(),
        },
        "junit": {
            "suite_count": 1,
            "testcase_count": len(nodeids),
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "output_name": junit.name,
            "sha256": release_gate._sha256_file(junit),
        },
    }


def _performance_evidence(*, successor: bool) -> dict:
    factory = (
        "src.migrations.v2.validation.copied_install_benchmark:create_successor_factory"
        if successor
        else "src.migrations.v2.validation.copied_install_benchmark:create_factory"
    )
    profile = "successor_release" if successor else "epoch_zero"
    pair = (
        "native_predecessor_vs_native_successor"
        if successor
        else "v1_compatibility_vs_native"
    )
    environment = {
        "os": "windows",
        "os_release": "10",
        "python_version": platform.python_version(),
        "faiss_version": "1.7",
        "numpy_version": "1.26",
        "cache_state": "warm",
        "reranker": "disabled-reader-parity",
        "query_input_sha256": "1" * 64,
        "dimension": 2,
        "metric": "l2",
        "k": 2,
    }
    if successor:
        environment.update(
            {
                "benchmark_pair": pair,
                "catalog_policy": "shared_checkpointed_catalog_clone_pinned_revisions",
                "baseline_snapshot_id": "a" * 64,
                "baseline_snapshot_sha256": "b" * 64,
                "baseline_ntotal": 5,
                "candidate_snapshot_id": "c" * 64,
                "candidate_snapshot_sha256": "d" * 64,
                "candidate_ntotal": 4,
                "write_epoch": 1,
                "v1_fallback_open": False,
            }
        )
    else:
        environment.update(
            {
                "active_snapshot_id": "a" * 64,
                "active_snapshot_sha256": "b" * 64,
                "ntotal": 5,
            }
        )
    provenance = {
        "schema_version": 1,
        "kind": "v2_retrieval_benchmark_provenance",
        "factory_entrypoint": factory,
        "adapter_callable": factory,
        "adapter_module_sha256": "2" * 64,
        "runner_entrypoint": "scripts.migrations.v2.run_v2_retrieval_benchmark:main",
        "runner_module_sha256": "3" * 64,
        "runtime_code_layout_sha256": "4" * 64,
        "runtime_code_file_count": 10,
        "layout_algorithm": "sha256-canonical-runtime-python-v1",
        "interpreter": {
            "implementation": platform.python_implementation().lower(),
            "version": platform.python_version(),
            "cache_tag": sys.implementation.cache_tag,
            "executable_sha256": "5" * 64,
        },
    }
    bootstrap = 10_000 if successor else 500
    analysis = {
        "schema_version": 1,
        "kind": "v2_retrieval_performance_analysis",
        "benchmark_pair": pair,
        "protocol": {
            "profile": profile,
            "required_workloads": list(release_gate.REQUIRED_WORKLOADS),
            "minimum_processes": 3,
            "process_count_policy": "exact" if successor else "minimum",
            "minimum_warmups": 10,
            "minimum_timed_samples_per_process_workload": (
                4_000 if successor else 200
            ),
            "minimum_fixed_queries_per_workload": 30,
            "minimum_bootstrap_resamples": bootstrap,
            "bootstrap_resamples": bootstrap,
            "bootstrap_seed": 20260716,
        },
        "workloads": {},
        "passed": True,
    }
    return {
        "schema_version": 1,
        "kind": "v2_retrieval_performance_evidence",
        "raw": {
            "schema_version": 1,
            "kind": "v1_v2_paired_retrieval_samples",
            "protocol_profile": profile,
            "provenance": provenance,
            "environment": environment,
            "processes": [],
        },
        "analysis": analysis,
    }


def test_release_gate_stays_pending_until_three_bound_approvals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary_paths = {
        label: _make_readonly(
            _write_json(tmp_path / f"{label}.json", {"label": label})
        )
        for label in release_gate.PRIMARY_LABELS
    }
    monkeypatch.setattr(
        release_gate,
        "_PRIMARY_VALIDATORS",
        {
            label: (
                lambda _value, _path, _context, expected=label: {
                    "kind": f"fixture_{expected}",
                    "label": expected,
                }
            )
            for label in release_gate.PRIMARY_LABELS
        },
    )

    candidate = release_gate.build_release_gate(primary_paths)

    assert candidate["kind"] == "v2_release_candidate_manifest"
    assert candidate["passed"] is True
    assert candidate["release_eligible"] is False
    assert candidate["pending_review_roles"] == [
        "architect",
        "critic",
        "verifier",
    ]
    bundle_sha256 = candidate["release_bundle_sha256"]
    v1_bundle_sha256 = hashlib.sha256(
        release_gate.canonical_json(
            {
                "schema_version": 1,
                "artifacts": {
                    label: {
                        "kind": candidate["artifacts"][label]["kind"],
                        "sha256": candidate["artifacts"][label]["sha256"],
                    }
                    for label in release_gate.PRIMARY_LABELS
                },
            }
        ).encode("utf-8")
    ).hexdigest()
    assert v1_bundle_sha256 != bundle_sha256
    with pytest.raises(release_gate.ReleaseGateError, match="bundle"):
        release_gate._validate_review_approval(
            _approval("architect", v1_bundle_sha256),
            "architect",
            bundle_sha256,
        )
    approvals = {
        role: _write_json(
            tmp_path / f"{role}.json", _approval(role, bundle_sha256)
        )
        for role in release_gate.REQUIRED_REVIEW_ROLES
    }
    for path in approvals.values():
        _make_readonly(path)

    final = release_gate.build_release_gate(primary_paths, approvals=approvals)

    assert final["kind"] == "v2_aggregate_release_gate"
    assert final["passed"] is True
    assert final["release_eligible"] is True
    assert set(final["approvals"]) == set(release_gate.REQUIRED_REVIEW_ROLES)

    assert candidate["schema_version"] == 2
    os.chmod(
        primary_paths["installed_validation"],
        stat.S_IREAD | stat.S_IWRITE,
    )
    primary_paths["installed_validation"].write_text(
        '{"label":"changed"}', encoding="utf-8"
    )
    _make_readonly(primary_paths["installed_validation"])
    with pytest.raises(release_gate.ReleaseGateError, match="bundle"):
        release_gate.build_release_gate(primary_paths, approvals=approvals)


def test_release_gate_rejects_primary_bytes_changed_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary_paths = {
        label: _make_readonly(
            _write_json(tmp_path / f"{label}.json", {"label": label})
        )
        for label in release_gate.PRIMARY_LABELS
    }

    def validator(_value, path, _context):
        if path == primary_paths["conversion"]:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            path.write_text('{"label":"replaced"}', encoding="utf-8")
            _make_readonly(path)
        return {"kind": "fixture"}

    monkeypatch.setattr(
        release_gate,
        "_PRIMARY_VALIDATORS",
        {label: validator for label in release_gate.PRIMARY_LABELS},
    )

    with pytest.raises(release_gate.ReleaseGateError, match="changed|stable"):
        release_gate.build_release_gate(primary_paths)


def test_reader_parity_validator_uses_the_gate_c_workload_contract():
    payload = _reader_parity_payload()
    context = {
        "conversion": {
            "validation": {
                "snapshot_id": "a" * 64,
                "snapshot_sha256": "b" * 64,
                "chunk_count": 8,
            }
        }
    }

    summary = release_gate._validate_reader_parity(
        payload,
        Path("reader-parity.json"),
        context,
    )

    assert summary["workload_count"] == 8
    payload["workloads"]["near_universe"] = payload["workloads"].pop("company")
    with pytest.raises(release_gate.ReleaseGateError, match="workload"):
        release_gate._validate_reader_parity(
            payload,
            Path("reader-parity.json"),
            context,
        )


def test_compatibility_validator_recomputes_the_exception_contract():
    payload = _compatibility_payload()
    context = _compatibility_context()

    summary = release_gate._validate_compatibility(
        payload,
        Path("compatibility.json"),
        context,
    )

    assert summary["active_descriptor"]["sha256"] == "d" * 64
    assert summary["predecessor_descriptor"]["sha256"] == "b" * 64
    assert summary["gate_d_expected_report_uid"] == "3" * 64
    mutations = (
        (
            lambda value: value["retrieval_quality"].update(
                predecessor_vector_queries=True,
                expected_content_rank_one=True,
                expected_content_within_k=True,
                source_top_one_equal=True,
                citation_complete_top_one=True,
            ),
            "compatibility|quality",
        ),
        (
            lambda value: value["structural_delta"].update(
                removed_duplicate_occurrences=2
            ),
            "structural|multiplicity|compatibility",
        ),
        (
            lambda value: value["retrieval_quality"].update(
                minimum_source_set_recall_at_k=1.1
            ),
            "quality|recall|compatibility",
        ),
        (
            lambda value: value["proposed_exception"].update(
                reason_codes=["unsupported_exception"]
            ),
            "exception|compatibility",
        ),
    )
    for mutate, message in mutations:
        bad = json.loads(json.dumps(payload))
        mutate(bad)
        with pytest.raises(release_gate.ReleaseGateError, match=message):
            release_gate._validate_compatibility(
                bad,
                Path("compatibility.json"),
                context,
            )


def test_compatibility_approval_binds_exact_exceptions_and_invariants():
    payload = _compatibility_payload()
    compatibility_sha256 = "f" * 64
    summary = release_gate._validate_compatibility(
        payload,
        Path("compatibility.json"),
        _compatibility_context(),
    )
    context = {
        "compatibility": {
            "name": "compatibility.json",
            "sha256": compatibility_sha256,
            "validation": summary,
        }
    }
    approval = _compatibility_approval(compatibility_sha256)

    assert release_gate._validate_compatibility_approval(
        approval,
        Path("approval.json"),
        context,
    )["approved"] is True

    mutations = (
        (
            lambda value: value.update(
                approved_exceptions=["unsupported_exception"]
            ),
            "approval|exception|evidence",
        ),
        (
            lambda value: value["required_invariants"].update(
                expected_content_rank_one=True
            ),
            "approval|invariant|evidence",
        ),
        (
            lambda value: value.update(reviewed_at_utc="not-a-timestamp"),
            "timestamp|approval",
        ),
        (
            lambda value: value["source_evidence"].update(
                relative_path="../compatibility.json"
            ),
            "source|approval|evidence",
        ),
    )
    for mutate, message in mutations:
        bad = json.loads(json.dumps(approval))
        mutate(bad)
        with pytest.raises(release_gate.ReleaseGateError, match=message):
            release_gate._validate_compatibility_approval(
                bad,
                Path("approval.json"),
                context,
            )


def test_performance_validator_binds_profiles_provenance_and_snapshot_descriptors(
    monkeypatch: pytest.MonkeyPatch,
):
    def reproduce(raw: dict, *, bootstrap_resamples: int, bootstrap_seed: int) -> dict:
        successor = raw["protocol_profile"] == "successor_release"
        evidence = _performance_evidence(successor=successor)
        assert bootstrap_resamples > 0
        assert bootstrap_seed == 20260716
        return evidence["analysis"]

    monkeypatch.setattr(release_gate, "analyze_benchmark", reproduce)
    monkeypatch.setattr(
        release_gate,
        "verify_current_benchmark_provenance",
        lambda *_args, **_kwargs: None,
    )
    context = {
        "conversion": {
            "validation": {
                "snapshot_id": "a" * 64,
                "snapshot_sha256": "b" * 64,
                "chunk_count": 5,
            }
        },
        "reader_parity": {
            "validation": {
                "snapshot_id": "a" * 64,
                "snapshot_sha256": "b" * 64,
                "dimension": 2,
                "metric": "l2",
                "ntotal": 5,
            }
        },
        "compatibility": {
            "validation": {
                "active_snapshot_id": "c" * 64,
                "predecessor_snapshot_id": "a" * 64,
                "write_epoch": 1,
                "active_descriptor": {
                    "snapshot_id": "c" * 64,
                    "sha256": "d" * 64,
                    "dimension": 2,
                    "metric": "l2",
                    "ntotal": 4,
                },
                "predecessor_descriptor": {
                    "snapshot_id": "a" * 64,
                    "sha256": "b" * 64,
                    "dimension": 2,
                    "metric": "l2",
                    "ntotal": 5,
                },
            }
        },
    }
    epoch = _performance_evidence(successor=False)
    epoch_summary = release_gate._validate_epoch_zero_performance(
        epoch,
        Path("epoch.json"),
        context,
    )
    context["epoch_zero_performance"] = {"validation": epoch_summary}
    successor = _performance_evidence(successor=True)

    successor_summary = release_gate._validate_successor_performance(
        successor,
        Path("successor.json"),
        context,
    )

    assert epoch_summary["protocol_profile"] == "epoch_zero"
    assert successor_summary["protocol_profile"] == "successor_release"
    mutations = (
        (
            lambda value: value["raw"]["environment"].update(
                candidate_snapshot_sha256="f" * 64
            ),
            "snapshot|pair|performance",
        ),
        (
            lambda value: value["raw"]["environment"].update(
                catalog_policy="untrusted_clone"
            ),
            "catalog|performance",
        ),
        (
            lambda value: value["raw"]["provenance"].update(
                factory_entrypoint="tests.synthetic:create_factory",
                adapter_callable="tests.synthetic:create_factory",
            ),
            "factory|provenance|performance",
        ),
        (
            lambda value: value["raw"].update(protocol_profile="epoch_zero"),
            "profile|pair|performance",
        ),
    )
    for mutate, message in mutations:
        bad = json.loads(json.dumps(successor))
        mutate(bad)
        with pytest.raises(release_gate.ReleaseGateError, match=message):
            release_gate._validate_successor_performance(
                bad,
                Path("successor.json"),
                context,
            )


def test_successor_race_and_launcher_matrix_reject_passed_shells():
    context = {
        "conversion": {
            "validation": {
                "snapshot_id": "a" * 64,
                "snapshot_sha256": "b" * 64,
                "chunk_count": 8,
            }
        }
    }
    race = {
        "schema_version": 1,
        "kind": "v2_first_successor_execution_replay",
        "passed": True,
        "candidate": {"snapshot_id": "c" * 64},
        "launcher_race": {"passed": True, "release_eligible": True},
        "publication": {
            "active_snapshot_id": "c" * 64,
            "predecessor_snapshot_id": "a" * 64,
            "write_epoch": 1,
            "v1_fallback_open": False,
        },
        "replay": {"embedding_api_calls": 0},
    }
    with pytest.raises(release_gate.ReleaseGateError, match="candidate|race"):
        release_gate._validate_successor_race(race, Path("race.json"), context)

    matrix = {
        "schema_version": 1,
        "kind": "v2_retrieval_launcher_matrix",
        "passed": True,
        "environment": {},
        "requirements": {
            "windows_required": True,
            "non_admin_required": True,
        },
        "cases_consistent": True,
        "cases": [{"passed": True}] * 3,
    }
    with pytest.raises(release_gate.ReleaseGateError, match="environment|requirements|case"):
        release_gate._validate_launcher_matrix(
            matrix,
            Path("matrix.json"),
            {
                "successor_race": {
                    "validation": {
                        "active_runtime_identity": {},
                        "launcher_layout_sha256": "d" * 64,
                    }
                }
            },
        )


def test_concurrent_post_publication_wave_requires_a_selected_runtime():
    surfaces = (
        "launcher_guard",
        "update_guard",
        "gui",
        "cli",
        "quick_start",
        "run_app_bat",
        "run_quickstart_bat",
    )
    blocked_wave = [
        {
            "label": label,
            "surface": surface,
            "exit_code": 1,
            "duration_ns": 1,
            "output_sha256": "a" * 64,
            "disposition": "blocked",
            "runtime_identity": None,
        }
        for label in ("source-default", "packaged-default")
        for surface in surfaces
    ]

    with pytest.raises(release_gate.ReleaseGateError, match="concurrent|selected"):
        release_gate._validate_installed_launcher_wave(
            blocked_wave,
            "after_concurrent",
            {"write_epoch": 0},
            {"write_epoch": 1},
        )

    after = {"write_epoch": 1}
    selected_wave = json.loads(json.dumps(blocked_wave))
    for outcome in selected_wave:
        outcome.update(
            exit_code=0,
            disposition="selected",
            runtime_identity=after,
        )
    selected_wave[0]["exit_code"] = False
    with pytest.raises(
        release_gate.ReleaseGateError,
        match="topology|exit|disposition",
    ):
        release_gate._validate_installed_launcher_wave(
            selected_wave,
            "after_concurrent",
            {"write_epoch": 0},
            after,
        )


def _installed_validation_context() -> dict:
    return {
        "launcher_matrix": {
            "validation": {"launcher_layout_sha256": "4" * 64}
        },
        "query_spec": {
            "sha256": "1" * 64,
            "validation": {
                "kind": "v2_release_semantic_query",
                "query_id": "gate-d",
                "expected_report_uid": "a" * 64,
                "query_text_sha256": "4" * 64,
                "vector_sha256": "5" * 64,
                "expected_citation_sha256": "6" * 64,
                "attestation_sha256": "7" * 64,
                "model": "baai/bge-m3",
            },
        },
        "transition": {
            "sha256": "2" * 64,
            "validation": {
                "kind": "v2_copied_install_release_transitions",
                "run_id": "transition-run",
                "query_spec_sha256": "1" * 64,
                "protected_tree_sha256_after": "b" * 64,
                "source_tree_sha256_after": "c" * 64,
                "final_runtime_identity": {
                    "active_snapshot_id": "d" * 64,
                    "predecessor_snapshot_id": "e" * 64,
                    "publication_generation": 5,
                    "write_epoch": 3,
                    "v1_fallback_open": False,
                    "degraded": False,
                    "write_enabled": True,
                },
            },
        },
    }

def _installed_validation_payload() -> dict:
    context = _installed_validation_context()
    query = context["query_spec"]["validation"]
    transition = context["transition"]["validation"]
    runtime_identity = {
        "mode": "native",
        **transition["final_runtime_identity"],
    }
    active_snapshot_sha256 = "8" * 64
    predecessor_snapshot_sha256 = "9" * 64
    baseline = {
        "runtime_identity": runtime_identity,
        "catalog_sha256": "d" * 64,
        "catalog_logical_sha256": "e" * 64,
        "snapshot_sha256": active_snapshot_sha256,
        "snapshots": {
            "active": {
                "snapshot_id": runtime_identity["active_snapshot_id"],
                "relative_path": "retrieval/v2/snapshots/active.faiss",
                "sha256": active_snapshot_sha256,
                "size_bytes": 100,
                "dimension": 2,
                "metric": "l2",
                "ntotal": 8,
            },
            "predecessor": {
                "snapshot_id": runtime_identity["predecessor_snapshot_id"],
                "relative_path": "retrieval/v2/snapshots/predecessor.faiss",
                "sha256": predecessor_snapshot_sha256,
                "size_bytes": 90,
                "dimension": 2,
                "metric": "l2",
                "ntotal": 7,
            },
        },
        "writer_lock": False,
        "staging_entries": 0,
    }
    packages = {name: "1.0" for name in REQUIRED_DISTRIBUTIONS}
    semantic_environment = {
        "python_version": "3.10.11",
        "implementation": "CPython",
        "packages": packages,
    }
    semantic_sha256 = hashlib.sha256(
        release_gate.canonical_json(semantic_environment).encode("utf-8")
    ).hexdigest()
    installed_environments = {
        label: {
            **semantic_environment,
            "python_executable_sha256": "f" * 64,
            "semantic_sha256": semantic_sha256,
        }
        for label in ("source-default", "packaged-default")
    }
    installed_environments_sha256 = hashlib.sha256(
        release_gate.canonical_json(installed_environments).encode("utf-8")
    ).hexdigest()
    expected_samples = 5
    timings = {
        "scope_compile": 1,
        "eligibility": 1,
        "faiss": 1,
        "hydration": 1,
        "lease": 1,
        "total": 5,
    }
    workloads = {}
    for name in release_gate.REQUIRED_WORKLOADS:
        empty = name == "empty"
        strategy = (
            "empty"
            if empty
            else "selector"
            if name in {"narrow", "prior_scope"}
            else "direct"
        )
        workloads[name] = {
            "samples": expected_samples,
            "strategies": [strategy] * expected_samples,
            "eligible_counts": [0 if empty else 8] * expected_samples,
            "faiss_calls": [0 if empty else 1] * expected_samples,
            "faiss_candidates": [0 if empty else 8] * expected_samples,
            "hydration_batches": [0 if empty else 1] * expected_samples,
            "hydration_rows": [0 if empty else 1] * expected_samples,
            "top_report_uids": [None if empty else query["expected_report_uid"]]
            * expected_samples,
            "top_chunk_uids": [None if empty else "3" * 64] * expected_samples,
            "citation_complete": [False if empty else True] * expected_samples,
            "citation_sha256": [
                None if empty else query["expected_citation_sha256"]
            ]
            * expected_samples,
            "timings_ns": [dict(timings) for _ in range(expected_samples)],
        }
    probe_runtime = {
        "active_snapshot_id": runtime_identity["active_snapshot_id"],
        "publication_generation": runtime_identity["publication_generation"],
        "active_build_id": "0" * 64,
        "profile_id": "1" * 64,
        "snapshot_sha256": active_snapshot_sha256,
        "ntotal": 8,
    }

    def probe(label: str) -> dict:
        return {
            "schema_version": 1,
            "kind": "v2_installed_validation_probe",
            "passed": True,
            "query_id": query["query_id"],
            "query_text_sha256": query["query_text_sha256"],
            "query_vector_sha256": query["vector_sha256"],
            "query_spec_sha256": context["query_spec"]["sha256"],
            "query_generation": {
                "provider": "openrouter",
                "model": query["model"],
                "input_type": "search_query",
                "provider_calls": 1,
                "attestation_sha256": query["attestation_sha256"],
            },
            "runtime_identity": probe_runtime,
            "workloads": workloads,
            "gate_d_search": {
                "expected_report_uid": query["expected_report_uid"],
                "top_report_uid": query["expected_report_uid"],
                "top_rank": 1,
                "citation_complete": True,
                "citation_sha256": query["expected_citation_sha256"],
            },
            "install": label,
            "duration_ns": 1,
            "output_sha256": "2" * 64,
        }

    guard_outcomes = [
        {
            "install": label,
            "write_guard": write_guard,
            "duration_ns": 1,
            "output_sha256": "3" * 64,
            "runtime_identity": runtime_identity,
        }
        for label in ("source-default", "packaged-default")
        for write_guard in (False, True)
    ]
    validation = {
        "recorded_at": "2026-07-18T00:01:00Z",
        "passed": True,
        "guard_outcomes": guard_outcomes,
        "search_probes": [
            probe("source-default"),
            probe("packaged-default"),
        ],
        "runtime_identity_sha256": hashlib.sha256(
            release_gate.canonical_json(runtime_identity).encode("utf-8")
        ).hexdigest(),
        "catalog_sha256": baseline["catalog_sha256"],
        "snapshot_sha256": baseline["snapshot_sha256"],
    }
    return {
        "schema_version": 1,
        "kind": "v2_installed_validation",
        "passed": True,
        "fixture_only": False,
        "release_eligible": False,
        "release_gate_pending": "aggregate_release_gate_manifest",
        "started_at": "2026-07-18T00:00:00Z",
        "completed_at": "2026-07-18T00:02:00Z",
        "environment": {
            "os": "windows",
            "os_release": "10",
            "python_version": "3.10.11",
            "non_admin": True,
        },
        "timeout_seconds": 60,
        "search_samples": expected_samples,
        "launcher_layout_sha256": "4" * 64,
        "installed_environments": installed_environments,
        "installed_environments_sha256": installed_environments_sha256,
        "retained_evidence": {
            "query": context["query_spec"]["sha256"],
            "transition": context["transition"]["sha256"],
        },
        "transition_run_id": transition["run_id"],
        "protected_tree_sha256": transition["protected_tree_sha256_after"],
        "source_tree_sha256": transition["source_tree_sha256_after"],
        "baseline": baseline,
        "final": baseline,
        "validation": validation,
    }

def test_installed_validation_binds_all_non_time_release_evidence():
    payload = _installed_validation_payload()
    context = _installed_validation_context()

    summary = release_gate._validate_installed_validation(
        payload,
        Path("installed-validation.json"),
        context,
    )

    assert summary["kind"] == "v2_installed_validation"
    assert summary["search_samples"] == 5
    assert summary["protected_tree_sha256"] == "b" * 64
    assert summary["source_tree_sha256"] == "c" * 64

    mutations = (
        (
            "query hash mismatch",
            lambda value: value["retained_evidence"].update(query="0" * 64),
        ),
        (
            "transition hash mismatch",
            lambda value: value["retained_evidence"].update(transition="0" * 64),
        ),
        (
            "transition run mismatch",
            lambda value: value.update(transition_run_id="different-run"),
        ),
        (
            "protected tree mismatch",
            lambda value: value.update(protected_tree_sha256="0" * 64),
        ),
        (
            "source tree mismatch",
            lambda value: value.update(source_tree_sha256="0" * 64),
        ),
        (
            "duplicate guard",
            lambda value: value["validation"]["guard_outcomes"].__setitem__(
                3,
                value["validation"]["guard_outcomes"][0],
            ),
        ),
        (
            "duplicate probe",
            lambda value: value["validation"]["search_probes"].__setitem__(
                1,
                value["validation"]["search_probes"][0],
            ),
        ),
        (
            "environment hash drift",
            lambda value: value.update(installed_environments_sha256="0" * 64),
        ),
        (
            "package drift",
            lambda value: value["installed_environments"][
                "packaged-default"
            ]["packages"].update(numpy="2.0"),
        ),
        (
            "launcher mismatch",
            lambda value: value.update(launcher_layout_sha256="0" * 64),
        ),
        (
            "runtime drift",
            lambda value: value["final"].update(catalog_sha256="0" * 64),
        ),
    )
    for _label, mutate in mutations:
        bad = json.loads(json.dumps(payload))
        mutate(bad)
        with pytest.raises(release_gate.ReleaseGateError, match="installed"):
            release_gate._validate_installed_validation(
                bad,
                Path("installed-validation.json"),
                context,
            )


def test_old_soak_artifact_kinds_are_rejected():
    installed = _installed_validation_payload()
    installed["kind"] = "v2_copied_install_soak"
    with pytest.raises(release_gate.ReleaseGateError, match="installed"):
        release_gate._validate_installed_validation(
            installed,
            Path("installed-validation.json"),
            _installed_validation_context(),
        )

    transition = _transition_evidence()
    transition["kind"] = "v2_copied_install_soak_transitions"
    context = {
        "compatibility": {
            "validation": {
                "active_snapshot_id": "c" * 64,
                "predecessor_snapshot_id": "a" * 64,
                "publication_generation": 2,
                "write_epoch": 1,
                "active_descriptor": {"sha256": "d" * 64},
                "predecessor_descriptor": {"sha256": "b" * 64},
            }
        },
        "query_spec": {
            "sha256": "e" * 64,
            "validation": {
                "query_id": "synthetic-skt-successor-query",
                "query_text_sha256": "4" * 64,
                "vector_sha256": "5" * 64,
                "expected_report_uid": "3" * 64,
                "expected_citation_sha256": "6" * 64,
                "model": "baai/bge-m3",
                "attestation_sha256": "7" * 64,
            },
        },
    }
    with pytest.raises(release_gate.ReleaseGateError, match="transition"):
        release_gate._validate_transition(transition, Path("transition.json"), context)


def test_transition_validator_binds_initial_pair_and_every_gate_d_hash():
    payload = _transition_evidence()
    context = {
        "compatibility": {
            "validation": {
                "active_snapshot_id": "c" * 64,
                "predecessor_snapshot_id": "a" * 64,
                "publication_generation": 2,
                "write_epoch": 1,
                "active_descriptor": {"sha256": "d" * 64},
                "predecessor_descriptor": {"sha256": "b" * 64},
            }
        },
        "query_spec": {
            "sha256": "e" * 64,
            "validation": {
                "query_id": "synthetic-skt-successor-query",
                "query_text_sha256": "4" * 64,
                "vector_sha256": "5" * 64,
                "expected_report_uid": "3" * 64,
                "expected_citation_sha256": "6" * 64,
                "attestation_sha256": "7" * 64,
                "model": "baai/bge-m3",
            },
        },
    }

    summary = release_gate._validate_transition(
        payload,
        Path("transition.json"),
        context,
    )

    assert summary["run_id"] == "release-transition-run"
    mutations = (
        (
            lambda value: value["initial"].update(active_snapshot_id="wrong"),
            "transition|initial",
        ),
        (
            lambda value: value["copy_proof"]["initial_snapshot_sha256"].update(active="0" * 64),
            "transition|initial",
        ),
        (
            lambda value: value["gate_d_search"].update(query_text_sha256="0" * 64),
            "transition|Gate D",
        ),
        (
            lambda value: value["gate_d_search"]["query_generation"].update(model="wrong"),
            "transition|Gate D",
        ),
    )
    for mutate, message in mutations:
        bad = json.loads(json.dumps(payload))
        mutate(bad)
        with pytest.raises(release_gate.ReleaseGateError, match=message):
            release_gate._validate_transition(
                bad,
                Path("transition.json"),
                context,
            )


def test_query_and_junit_validators_fail_closed(tmp_path: Path):
    query_text = "SK텔레콤 recovery"
    vector = [0.25, -0.5]
    vector_bytes = release_gate._float32_bytes(vector)
    query = {
        "schema_version": 1,
        "kind": "v2_release_semantic_query",
        "query_id": "gate-d",
        "query_text": query_text,
        "vector": vector,
        "k": 1,
        "expected_report_uid": "a" * 64,
        "expected_citation": {
            "canonical_relative_path": "downloaded/a.pdf",
            "report_type": "company",
            "report_date": "2026-07-13",
            "target_name": "SK텔레콤",
            "title": "Recovery",
            "broker": "Broker",
        },
        "scopes": {
            "unfiltered": None,
            "empty": {"empty": True},
            "narrow": {"target_name": "SK텔레콤"},
            "broad": {"report_type": "company"},
            "near_universe": {"report_date_start": "2026-01-01"},
            "prior_scope": {
                "prior_scope": {"canonical_relative_path": "downloaded/a.pdf"}
            },
        },
        "embedding_attestation": {
            "provider": "openrouter",
            "model": "baai/bge-m3",
            "input_type": "search_query",
            "provider_calls": 1,
            "query_text_sha256": hashlib.sha256(
                query_text.encode("utf-8")
            ).hexdigest(),
            "vector_sha256": hashlib.sha256(vector_bytes).hexdigest(),
        },
    }
    query_path = _write_json(tmp_path / "query.json", query)
    query_file_sha256 = hashlib.sha256(query_path.read_bytes()).hexdigest()
    context = {
        "compatibility": {
            "validation": {
                "active_descriptor": {"dimension": 2, "ntotal": 4},
                "gate_d_query_spec_sha256": query_file_sha256,
                "gate_d_expected_report_uid": "a" * 64,
            }
        }
    }
    summary = release_gate._validate_query(query, query_path, context)
    assert summary["expected_report_uid"] == "a" * 64
    assert summary["query_id"] == "gate-d"
    assert summary["expected_citation_sha256"] == hashlib.sha256(
        release_gate.canonical_json(query["expected_citation"]).encode("utf-8")
    ).hexdigest()
    assert summary["attestation_sha256"] == hashlib.sha256(
        release_gate.canonical_json(query["embedding_attestation"]).encode("utf-8")
    ).hexdigest()

    query["embedding_attestation"]["provider_calls"] = 0
    with pytest.raises(release_gate.ReleaseGateError, match="attestation"):
        release_gate._validate_query(query, query_path, context)
    query["embedding_attestation"]["provider_calls"] = 1
    malformed_queries = (
        (lambda value: value.update(query_id=""), "query"),
        (lambda value: value["vector"].__setitem__(0, True), "vector"),
        (lambda value: value.update(k=5), "dimension|contract"),
        (lambda value: value["scopes"].update(unfiltered={}), "workload"),
        (
            lambda value: value["embedding_attestation"].update(extra="field"),
            "attestation",
        ),
    )
    for mutate, message in malformed_queries:
        malformed = json.loads(json.dumps(query))
        mutate(malformed)
        with pytest.raises(release_gate.ReleaseGateError, match=message):
            release_gate._validate_query(malformed, query_path, context)

    mismatched_context = json.loads(json.dumps(context))
    mismatched_context["compatibility"]["validation"][
        "gate_d_query_spec_sha256"
    ] = "f" * 64
    with pytest.raises(release_gate.ReleaseGateError, match="compatibility|query"):
        release_gate._validate_query(query, query_path, mismatched_context)

    junit = tmp_path / "pytest.xml"
    junit.write_text(_junit_xml(), encoding="utf-8")
    assert release_gate._validate_junit(junit)["tests"] == 2
    junit.write_text(
        _junit_xml(declared_tests=1),
        encoding="utf-8",
    )
    with pytest.raises(release_gate.ReleaseGateError, match="summary|testcase"):
        release_gate._validate_junit(junit)


def test_release_manifest_output_cannot_change_attested_layouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    for name in ("tests", "src", "scripts", "apps", "reports"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(release_gate, "REPOSITORY_ROOT", tmp_path)

    for target in (
        tmp_path / "tests" / "new-output-directory" / "release-gate.json",
        tmp_path / "src" / "release_gate.py",
    ):
        with pytest.raises(release_gate.ReleaseGateError, match="source|test|layout"):
            release_gate._write_immutable_json(target, {"passed": True})
        assert not target.exists()
    assert not (tmp_path / "tests" / "new-output-directory").exists()


def test_release_manifest_output_rejects_a_reparse_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "reports" / "release-gate.json"
    monkeypatch.setattr(release_gate, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(
        release_gate.run_release_pytest,
        "_path_has_reparse_component",
        lambda _path: True,
    )

    with pytest.raises(release_gate.ReleaseGateError, match="unsafe|output"):
        release_gate._write_immutable_json(target, {"passed": True})
    assert not target.exists()


def test_pytest_attestation_binds_collection_layout_and_junit(tmp_path: Path):
    junit = tmp_path / "pytest.xml"
    junit.write_text(_junit_xml(), encoding="utf-8")
    attestation = _pytest_attestation(junit)
    attestation_path = _write_json(tmp_path / "pytest-attestation.json", attestation)

    summary = release_gate._validate_pytest_attestation(
        attestation,
        attestation_path,
        {},
    )
    context = {
        "pytest_attestation": {
            "name": attestation_path.name,
            "sha256": release_gate._sha256_file(attestation_path),
            "validation": summary,
        }
    }
    junit_summary = release_gate._validate_junit_artifact(
        junit.read_bytes(),
        junit,
        context,
    )

    assert summary["collection_count"] == 2
    assert junit_summary["tests"] == 2
    mutations = (
        (
            lambda value: value["protocol"].update(
                collection_exit_code=False,
                selection_args_allowed=0,
            ),
            "protocol|pytest",
        ),
        (
            lambda value: value["collection"].update(count=1),
            "collection|JUnit",
        ),
        (
            lambda value: value["collection"].update(nodeids_sha256="0" * 64),
            "nodeid|collection",
        ),
        (
            lambda value: value["commands"]["collection_argv"].append(
                "tests/test_partial.py"
            ),
            "command|selection|pytest",
        ),
        (
            lambda value: value["layouts"].update(source_layout_sha256="0" * 64),
            "layout",
        ),
    )
    for mutate, message in mutations:
        bad = json.loads(json.dumps(attestation))
        mutate(bad)
        with pytest.raises(release_gate.ReleaseGateError, match=message):
            release_gate._validate_pytest_attestation(
                bad,
                attestation_path,
                {},
            )

    mismatched_context = json.loads(json.dumps(context))
    mismatched_context["pytest_attestation"]["validation"]["junit_sha256"] = (
        "0" * 64
    )
    with pytest.raises(release_gate.ReleaseGateError, match="JUnit|attestation"):
        release_gate._validate_junit_artifact(
            junit.read_bytes(),
            junit,
            mismatched_context,
        )


def test_safe_file_rejects_a_symlinked_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_json(tmp_path / "artifact.json", {"passed": True})
    link = tmp_path / "artifact-link.json"
    original_is_symlink = Path.is_symlink

    def is_symlink(path: Path) -> bool:
        return path == link or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)

    with pytest.raises(release_gate.ReleaseGateError, match="unsafe"):
        release_gate._safe_file(link, "fixture")


def test_safe_file_requires_readonly_release_inputs(tmp_path: Path):
    artifact = _write_json(tmp_path / "artifact.json", {"passed": True})

    with pytest.raises(release_gate.ReleaseGateError, match="read-only"):
        release_gate._safe_file(artifact, "fixture", require_readonly=True)

    _make_readonly(artifact)
    assert release_gate._safe_file(
        artifact,
        "fixture",
        require_readonly=True,
    ) == artifact.resolve()


def test_final_review_approval_requires_a_real_utc_timestamp():
    approval = _approval("architect", "a" * 64)
    approval["reviewed_at_utc"] = "not-a-timestamp"

    with pytest.raises(release_gate.ReleaseGateError, match="timestamp"):
        release_gate._validate_review_approval(
            approval,
            "architect",
            "a" * 64,
        )
