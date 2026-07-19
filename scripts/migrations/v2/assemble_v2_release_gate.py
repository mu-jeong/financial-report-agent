"""Validate and seal the aggregate V2 retrieval release decision."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import stat
import struct
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from scripts.migrations.v2 import run_release_pytest
from scripts.migrations.v2.run_v2_reader_parity import (
    GATE_C_WORKLOADS as READER_PARITY_WORKLOADS,
    MISMATCH_FIELDS as READER_PARITY_MISMATCH_FIELDS,
)
from src.migrations.v2.validation.benchmark_provenance import (
    BenchmarkProvenanceError,
    verify_current_benchmark_provenance,
)
from src.migrations.v2.validation.installed_validation import (
    InstalledValidationError,
    validate_installed_validation as validate_installed_validation_evidence,
)
from src.migrations.v2.validation.performance import REQUIRED_WORKLOADS, analyze_benchmark
from src.migrations.v2.validation.release_transitions import (
    ReleaseTransitionError,
    validate_release_transition_evidence,
)
from src.retrieval.identity import canonical_json


PRIMARY_LABELS = (
    "conversion",
    "validation",
    "reader_parity",
    "successor_race",
    "launcher_matrix",
    "compatibility",
    "compatibility_approval",
    "epoch_zero_performance",
    "successor_performance",
    "query_spec",
    "transition",
    "installed_validation",
    "pytest_attestation",
    "pytest_junit",
)
REQUIRED_REVIEW_ROLES = ("architect", "critic", "verifier")
_HEX = frozenset("0123456789abcdef")


class ReleaseGateError(RuntimeError):
    """Raised when retained evidence cannot prove release eligibility."""


Validator = Callable[[Any, Path, Mapping[str, Any]], dict[str, Any]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble the fail-closed aggregate V2 retrieval release gate"
    )
    for label in PRIMARY_LABELS:
        parser.add_argument(
            "--" + label.replace("_", "-"),
            dest=label,
            type=Path,
            required=True,
        )
    parser.add_argument(
        "--approval",
        action="append",
        default=[],
        metavar="ROLE=PATH",
        help="final architect, critic, and verifier approval artifacts",
    )
    parser.add_argument(
        "--pending",
        action="store_true",
        help="seal the validated candidate bundle before independent review",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        approvals = _parse_approval_args(args.approval)
        if args.pending and approvals:
            raise ReleaseGateError("pending manifest cannot include approvals")
        if not args.pending and set(approvals) != set(REQUIRED_REVIEW_ROLES):
            raise ReleaseGateError(
                "final gate requires architect, critic, and verifier approvals"
            )
        primary = {label: getattr(args, label) for label in PRIMARY_LABELS}
        payload = build_release_gate(
            primary,
            approvals=None if args.pending else approvals,
        )
        output = _write_immutable_json(args.output, payload)
    except (OSError, ValueError, ET.ParseError, ReleaseGateError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": type(exc).__name__},
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": (
                    "release_eligible"
                    if payload["release_eligible"]
                    else "review_pending"
                ),
                "evidence": output.name,
                "release_bundle_sha256": payload["release_bundle_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


def build_release_gate(
    primary_paths: Mapping[str, str | Path],
    *,
    approvals: Mapping[str, str | Path] | None = None,
) -> dict[str, Any]:
    if set(primary_paths) != set(PRIMARY_LABELS):
        raise ReleaseGateError("primary release artifact set is incomplete")
    context: dict[str, Any] = {}
    entries: dict[str, dict[str, Any]] = {}
    for label in PRIMARY_LABELS:
        path = _safe_file(
            primary_paths[label],
            label,
            require_readonly=True,
        )
        encoded, artifact_sha256 = _read_artifact_bytes(path, label)
        value = (
            encoded
            if label == "pytest_junit"
            else _decode_json_artifact(encoded, label)
        )
        validation = _PRIMARY_VALIDATORS[label](value, path, context)
        _assert_artifact_unchanged(path, label, artifact_sha256)
        entry = {
            "name": _retained_name(path),
            "sha256": artifact_sha256,
            "kind": validation["kind"],
            "validation": validation,
        }
        entries[label] = entry
        context[label] = entry
    bundle_sha256 = _release_bundle_sha256(entries)
    common = {
        "schema_version": 2,
        "passed": True,
        "release_bundle_sha256": bundle_sha256,
        "artifacts": entries,
    }
    if approvals is None:
        return {
            **common,
            "kind": "v2_release_candidate_manifest",
            "release_eligible": False,
            "pending_review_roles": list(REQUIRED_REVIEW_ROLES),
        }
    if set(approvals) != set(REQUIRED_REVIEW_ROLES):
        raise ReleaseGateError(
            "final gate requires exactly three independent review roles"
        )
    approval_entries: dict[str, dict[str, Any]] = {}
    for role in REQUIRED_REVIEW_ROLES:
        path = _safe_file(
            approvals[role],
            f"{role} approval",
            require_readonly=True,
        )
        label = f"{role} approval"
        encoded, approval_sha256 = _read_artifact_bytes(path, label)
        value = _decode_json_artifact(encoded, label)
        summary = _validate_review_approval(value, role, bundle_sha256)
        _assert_artifact_unchanged(path, label, approval_sha256)
        approval_entries[role] = {
            "name": _retained_name(path),
            "sha256": approval_sha256,
            **summary,
        }
    return {
        **common,
        "kind": "v2_aggregate_release_gate",
        "release_eligible": True,
        "open_p0_count": 0,
        "open_p1_count": 0,
        "approvals": approval_entries,
    }


def _validate_conversion(
    value: Any,
    _path: Path,
    _context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "conversion")
    _exact_keys(obj, {"schema_version", "kind", "result"}, "conversion")
    if obj["schema_version"] != 1 or obj["kind"] != "v2_conversion_result":
        raise ReleaseGateError("conversion identity is invalid")
    result = _object(obj["result"], "conversion result")
    required = {
        "build_id",
        "catalog_relative_path",
        "chunk_count",
        "evidence_manifest_relative_path",
        "max_vector_absolute_error",
        "parent_count",
        "profile_hash",
        "publication_id",
        "report_count",
        "snapshot_id",
        "snapshot_relative_path",
        "snapshot_sha256",
        "snapshot_size_bytes",
    }
    _exact_keys(result, required, "conversion result")
    for field in ("build_id", "profile_hash", "publication_id", "snapshot_id", "snapshot_sha256"):
        _require_digest(result[field], f"conversion {field}")
    if (
        any(
            not isinstance(result[field], int)
            or isinstance(result[field], bool)
            or result[field] <= 0
            for field in ("chunk_count", "parent_count", "report_count", "snapshot_size_bytes")
        )
        or result["max_vector_absolute_error"] != 0.0
    ):
        raise ReleaseGateError("conversion counts or vector parity are invalid")
    return {
        "kind": obj["kind"],
        "snapshot_id": result["snapshot_id"],
        "snapshot_sha256": result["snapshot_sha256"],
        "report_count": result["report_count"],
        "parent_count": result["parent_count"],
        "chunk_count": result["chunk_count"],
    }


def _validate_conversion_validation(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "conversion validation")
    _exact_keys(
        obj,
        {"schema_version", "kind", "snapshot_id", "snapshot_sha256", "valid"},
        "conversion validation",
    )
    seed = context["conversion"]["validation"]
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_conversion_validation"
        or obj["valid"] is not True
        or obj["snapshot_id"] != seed["snapshot_id"]
        or obj["snapshot_sha256"] != seed["snapshot_sha256"]
    ):
        raise ReleaseGateError("conversion validation is not bound to the seed")
    return {
        "kind": obj["kind"],
        "snapshot_id": obj["snapshot_id"],
        "valid": True,
    }


def _validate_reader_parity(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "reader parity")
    _exact_keys(
        obj,
        {
            "schema_version",
            "kind",
            "protocol",
            "inputs",
            "runtime",
            "status",
            "counts",
            "mismatches",
            "workloads",
        },
        "reader parity",
    )
    inputs = _object(obj["inputs"], "reader parity inputs")
    _exact_keys(
        inputs,
        {"query_input_sha256", "scope_input_sha256"},
        "reader parity inputs",
    )
    for field, digest in inputs.items():
        _require_digest(digest, f"reader parity {field}")

    runtime = _object(obj["runtime"], "reader parity runtime")
    _exact_keys(
        runtime,
        {
            "snapshot_id",
            "snapshot_sha256",
            "publication_generation",
            "write_epoch",
            "dimension",
            "metric",
            "ntotal",
            "conversion_manifest_sha256",
            "legacy_mapping_sha256",
            "source_manifest_sha256",
        },
        "reader parity runtime",
    )
    for field in (
        "snapshot_id",
        "snapshot_sha256",
        "conversion_manifest_sha256",
        "legacy_mapping_sha256",
        "source_manifest_sha256",
    ):
        _require_digest(runtime[field], f"reader parity {field}")

    protocol = _object(obj["protocol"], "reader parity protocol")
    _exact_keys(
        protocol,
        {
            "query_count",
            "workload_count",
            "request_count",
            "k",
            "exact_score_tie_policy",
            "l2_order",
            "inner_product_order",
        },
        "reader parity protocol",
    )
    mismatches = _object(obj["mismatches"], "reader parity mismatches")
    _exact_keys(
        mismatches,
        set(READER_PARITY_MISMATCH_FIELDS),
        "reader parity mismatches",
    )
    counts = _object(obj["counts"], "reader parity counts")
    count_fields = {
        "legacy_eligible_count",
        "legacy_exact_score_tie_groups",
        "native_eligible_count",
        "native_exact_score_tie_groups",
    }
    _exact_keys(counts, count_fields, "reader parity counts")
    workloads = _object(obj["workloads"], "reader parity workloads")
    if set(workloads) != set(READER_PARITY_WORKLOADS):
        raise ReleaseGateError("reader parity workload set is invalid")
    workload_fields = count_fields | {"mismatches", "passed", "request_count"}
    seed = context["conversion"]["validation"]
    integer_values = (
        protocol["query_count"],
        protocol["workload_count"],
        protocol["request_count"],
        protocol["k"],
        runtime["publication_generation"],
        runtime["write_epoch"],
        runtime["dimension"],
        runtime["ntotal"],
    )
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v1_v2_copied_install_reader_parity"
        or obj["status"] != "passed"
        or any(value != 0 for value in mismatches.values())
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in integer_values
        )
        or protocol["query_count"] <= 0
        or protocol["workload_count"] != len(READER_PARITY_WORKLOADS)
        or protocol["request_count"]
        != protocol["query_count"] * len(READER_PARITY_WORKLOADS)
        or not 0 < protocol["k"] <= runtime["ntotal"]
        or protocol["exact_score_tie_policy"]
        != "score_group_then_chunk_uid_before_top_k"
        or protocol["l2_order"]
        != "ascending_score_then_chunk_uid_for_exact_ties"
        or protocol["inner_product_order"]
        != "descending_score_then_chunk_uid_for_exact_ties"
        or runtime["snapshot_id"] != seed["snapshot_id"]
        or runtime["snapshot_sha256"] != seed["snapshot_sha256"]
        or runtime["publication_generation"] <= 0
        or runtime["write_epoch"] != 0
        or runtime["dimension"] <= 0
        or runtime["metric"] not in {"l2", "inner_product"}
        or runtime["ntotal"] != seed["chunk_count"]
        or any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            for count in counts.values()
        )
        or counts.get("legacy_eligible_count") != counts.get("native_eligible_count")
        or counts.get("legacy_exact_score_tie_groups")
        != counts.get("native_exact_score_tie_groups")
    ):
        raise ReleaseGateError("reader parity contains a mismatch")

    workload_totals = {field: 0 for field in count_fields}
    for name in READER_PARITY_WORKLOADS:
        workload = _object(workloads[name], f"reader parity {name} workload")
        _exact_keys(workload, workload_fields, f"reader parity {name} workload")
        workload_mismatches = _object(
            workload["mismatches"],
            f"reader parity {name} mismatches",
        )
        _exact_keys(
            workload_mismatches,
            set(READER_PARITY_MISMATCH_FIELDS),
            f"reader parity {name} mismatches",
        )
        workload_counts = [workload[field] for field in count_fields]
        if (
            workload["passed"] is not True
            or workload["request_count"] != protocol["query_count"]
            or any(value != 0 for value in workload_mismatches.values())
            or any(
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
                for count in workload_counts
            )
            or workload["legacy_eligible_count"]
            != workload["native_eligible_count"]
            or workload["legacy_exact_score_tie_groups"]
            != workload["native_exact_score_tie_groups"]
        ):
            raise ReleaseGateError(f"reader parity {name} workload contains a mismatch")
        for field in count_fields:
            workload_totals[field] += workload[field]
    if workload_totals != counts:
        raise ReleaseGateError("reader parity aggregate counts do not match workloads")
    return {
        "kind": obj["kind"],
        "snapshot_id": runtime["snapshot_id"],
        "snapshot_sha256": runtime["snapshot_sha256"],
        "dimension": runtime["dimension"],
        "metric": runtime["metric"],
        "ntotal": runtime["ntotal"],
        "workload_count": len(READER_PARITY_WORKLOADS),
        "mismatch_count": 0,
    }


def _validate_successor_race(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "successor race")
    _exact_keys(
        obj,
        {"schema_version", "kind", "passed", "candidate", "launcher_race", "publication", "replay"},
        "successor race",
    )
    candidate = _object(obj["candidate"], "successor candidate")
    launcher = _object(obj["launcher_race"], "launcher race")
    publication = _object(obj["publication"], "successor publication")
    replay = _object(obj["replay"], "successor replay")
    _exact_keys(
        candidate,
        {
            "build_id",
            "snapshot_id",
            "report_count",
            "parent_count",
            "chunk_count",
            "evidence_manifest_sha256",
        },
        "successor candidate",
    )
    _exact_keys(
        publication,
        {
            "publication_id",
            "publication_generation",
            "write_epoch",
            "active_snapshot_id",
            "predecessor_snapshot_id",
            "v1_fallback_open",
            "checkpoint_sha256",
        },
        "successor publication",
    )
    _exact_keys(
        replay,
        {
            "candidate_manifest_sha256",
            "candidate_reused",
            "embedding_api_calls",
            "reason",
            "source_publication_id",
        },
        "successor replay",
    )
    for field in ("build_id", "snapshot_id", "evidence_manifest_sha256"):
        _require_digest(candidate[field], f"successor candidate {field}")
    for field in ("publication_id", "active_snapshot_id", "predecessor_snapshot_id", "checkpoint_sha256"):
        _require_digest(publication[field], f"successor publication {field}")
    _require_digest(replay["candidate_manifest_sha256"], "successor replay manifest")
    _require_digest(replay["source_publication_id"], "successor replay publication")
    seed = context["conversion"]["validation"]
    candidate_counts = (
        candidate["report_count"],
        candidate["parent_count"],
        candidate["chunk_count"],
    )
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_first_successor_execution_replay"
        or obj["passed"] is not True
        or any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            for count in candidate_counts
        )
        or not candidate["report_count"] <= candidate["parent_count"] <= candidate["chunk_count"]
        or publication["predecessor_snapshot_id"] != seed["snapshot_id"]
        or publication["active_snapshot_id"] != candidate["snapshot_id"]
        or publication["active_snapshot_id"] == publication["predecessor_snapshot_id"]
        or publication["publication_generation"] != 2
        or publication["write_epoch"] != 1
        or publication["v1_fallback_open"] is not False
        or replay["candidate_manifest_sha256"] != candidate["evidence_manifest_sha256"]
        or replay["candidate_reused"] is not True
        or replay["embedding_api_calls"] != 0
        or isinstance(replay["embedding_api_calls"], bool)
        or not isinstance(replay["reason"], str)
        or not replay["reason"].strip()
        or replay["source_publication_id"] != publication["publication_id"]
    ):
        raise ReleaseGateError("first successor race evidence is invalid")
    launcher_summary = _validate_launcher_race(
        launcher,
        seed_snapshot_id=seed["snapshot_id"],
        active_snapshot_id=candidate["snapshot_id"],
        publication_id=publication["publication_id"],
        publication_generation=publication["publication_generation"],
        write_epoch=publication["write_epoch"],
    )
    return {
        "kind": obj["kind"],
        "active_snapshot_id": candidate["snapshot_id"],
        "predecessor_snapshot_id": seed["snapshot_id"],
        "candidate_build_id": candidate["build_id"],
        "candidate_manifest_sha256": candidate["evidence_manifest_sha256"],
        "candidate_report_count": candidate["report_count"],
        "candidate_parent_count": candidate["parent_count"],
        "candidate_chunk_count": candidate["chunk_count"],
        "publication_id": publication["publication_id"],
        "publication_generation": publication["publication_generation"],
        "write_epoch": publication["write_epoch"],
        **launcher_summary,
    }


def _validate_launcher_matrix(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "launcher matrix")
    _exact_keys(
        obj,
        {"schema_version", "kind", "passed", "environment", "requirements", "cases", "cases_consistent"},
        "launcher matrix",
    )
    environment = _object(obj["environment"], "launcher environment")
    _exact_keys(
        environment,
        {"os", "os_release", "python_version", "non_admin"},
        "launcher environment",
    )
    requirements = _object(obj["requirements"], "launcher requirements")
    _exact_keys(
        requirements,
        {
            "windows_required",
            "non_admin_required",
            "case_labels",
            "post_successor_state",
            "cross_case_runtime_identity_required",
            "cross_case_catalog_hash_required",
            "case_specific_install_roots_required",
            "cross_case_launcher_layout_hash_required",
            "surfaces",
        },
        "launcher requirements",
    )
    case_labels = ("source-default", "packaged-default", "custom-local")
    surfaces = (
        "launcher_guard",
        "gui",
        "cli",
        "quick_start",
        "run_app_bat",
        "run_quickstart_bat",
    )
    post_successor = _object(
        requirements["post_successor_state"],
        "launcher post-successor state",
    )
    _exact_keys(
        post_successor,
        {"mode", "write_epoch", "v1_fallback_open", "degraded", "write_enabled"},
        "launcher post-successor state",
    )
    race = context["successor_race"]["validation"]
    expected_runtime = dict(race["active_runtime_identity"])
    expected_runtime.pop("mode")
    cases = obj["cases"]
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_retrieval_launcher_matrix"
        or obj["passed"] is not True
        or obj["cases_consistent"] is not True
        or not isinstance(cases, list)
        or len(cases) != len(case_labels)
        or environment["os"] != "windows"
        or environment["non_admin"] is not True
        or any(
            not isinstance(environment[field], str) or not environment[field]
            for field in ("os_release", "python_version")
        )
        or requirements.get("windows_required") is not True
        or requirements.get("non_admin_required") is not True
        or requirements["case_labels"] != list(case_labels)
        or requirements["surfaces"] != list(surfaces)
        or requirements["cross_case_runtime_identity_required"] is not True
        or requirements["cross_case_catalog_hash_required"] is not True
        or requirements["case_specific_install_roots_required"] is not True
        or requirements["cross_case_launcher_layout_hash_required"] is not True
        or canonical_json(post_successor)
        != canonical_json(
            {
                "mode": "native",
                "write_epoch": "positive",
                "v1_fallback_open": False,
                "degraded": False,
                "write_enabled": True,
            }
        )
    ):
        raise ReleaseGateError("launcher matrix is incomplete")
    catalog_hashes: set[str] = set()
    layout_hashes: set[str] = set()
    for expected_label, case_value in zip(case_labels, cases):
        case = _object(case_value, f"launcher {expected_label} case")
        _exact_keys(
            case,
            {
                "label",
                "launcher_layout_sha256",
                "catalog_sha256",
                "catalog_unchanged",
                "runtime_identity",
                "passed",
                "surfaces",
            },
            f"launcher {expected_label} case",
        )
        if (
            case["label"] != expected_label
            or case["passed"] is not True
            or case["catalog_unchanged"] is not True
            or canonical_json(case["runtime_identity"])
            != canonical_json(expected_runtime)
        ):
            raise ReleaseGateError("launcher case runtime is invalid")
        catalog_hashes.add(
            _require_digest(case["catalog_sha256"], "launcher catalog")
        )
        layout_hashes.add(
            _require_digest(case["launcher_layout_sha256"], "launcher layout")
        )
        surface_values = case["surfaces"]
        if not isinstance(surface_values, list) or len(surface_values) != len(surfaces):
            raise ReleaseGateError("launcher case surfaces are incomplete")
        runtime_sha256 = hashlib.sha256(
            canonical_json(expected_runtime).encode("utf-8")
        ).hexdigest()
        for expected_surface, surface_value in zip(surfaces, surface_values):
            surface = _object(surface_value, f"launcher {expected_surface} surface")
            _exact_keys(
                surface,
                {
                    "surface",
                    "exit_code",
                    "duration_ns",
                    "output_sha256",
                    "runtime_identity_sha256",
                    "passed",
                },
                f"launcher {expected_surface} surface",
            )
            if (
                surface["surface"] != expected_surface
                or not isinstance(surface["exit_code"], int)
                or isinstance(surface["exit_code"], bool)
                or surface["exit_code"] != 0
                or surface["passed"] is not True
                or not isinstance(surface["duration_ns"], int)
                or isinstance(surface["duration_ns"], bool)
                or surface["duration_ns"] <= 0
                or surface["runtime_identity_sha256"] != runtime_sha256
            ):
                raise ReleaseGateError("launcher case surface result is invalid")
            _require_digest(surface["output_sha256"], "launcher surface output")
    if (
        len(catalog_hashes) != 1
        or layout_hashes != {race["launcher_layout_sha256"]}
    ):
        raise ReleaseGateError("launcher matrix cross-case identity differs")
    return {
        "kind": obj["kind"],
        "case_count": len(cases),
        "passed": True,
        "catalog_sha256": next(iter(catalog_hashes)),
        "launcher_layout_sha256": race["launcher_layout_sha256"],
        "runtime_identity": expected_runtime,
    }


def _validate_launcher_race(
    value: Mapping[str, Any],
    *,
    seed_snapshot_id: str,
    active_snapshot_id: str,
    publication_id: str,
    publication_generation: int,
    write_epoch: int,
) -> dict[str, Any]:
    _exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "worker_count",
            "probe_count",
            "observation_counts",
            "observed_runtime_identity_sha256",
            "before",
            "after",
            "publication_id",
            "installed_process_waves",
            "release_eligible",
            "passed",
        },
        "launcher race",
    )
    before = {
        "mode": "native",
        "active_snapshot_id": seed_snapshot_id,
        "publication_generation": 1,
        "write_epoch": 0,
        "v1_fallback_open": True,
        "degraded": False,
        "write_enabled": False,
    }
    after = {
        "mode": "native",
        "active_snapshot_id": active_snapshot_id,
        "publication_generation": publication_generation,
        "write_epoch": write_epoch,
        "v1_fallback_open": False,
        "degraded": False,
        "write_enabled": True,
    }
    observations = _object(value["observation_counts"], "launcher observations")
    _exact_keys(
        observations,
        {
            "launcher:fail_closed",
            "launcher:selected",
            "updater:fail_closed",
            "updater:selected",
        },
        "launcher observations",
    )
    if (
        value["schema_version"] != 1
        or value["kind"] != "v2_first_successor_launcher_race"
        or value["passed"] is not True
        or value["release_eligible"] is not True
        or value["publication_id"] != publication_id
        or canonical_json(value["before"]) != canonical_json(before)
        or canonical_json(value["after"]) != canonical_json(after)
        or not isinstance(value["worker_count"], int)
        or isinstance(value["worker_count"], bool)
        or value["worker_count"] <= 0
        or not isinstance(value["probe_count"], int)
        or isinstance(value["probe_count"], bool)
        or value["probe_count"] <= 0
        or any(
            not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            for count in observations.values()
        )
        or sum(observations.values()) != value["probe_count"]
    ):
        raise ReleaseGateError("launcher race observations are invalid")
    expected_identity_hashes = sorted(
        _launcher_runtime_tuple_sha256(identity) for identity in (before, after)
    )
    if value["observed_runtime_identity_sha256"] != expected_identity_hashes:
        raise ReleaseGateError("launcher race observed runtime identities are invalid")
    waves = _object(value["installed_process_waves"], "installed launcher waves")
    _exact_keys(
        waves,
        {"launcher_layout_sha256", "before", "race", "after_concurrent", "after"},
        "installed launcher waves",
    )
    layouts = _object(waves["launcher_layout_sha256"], "launcher race layouts")
    _exact_keys(layouts, {"source-default", "packaged-default"}, "launcher race layouts")
    layout_values = {
        _require_digest(layout, "launcher race layout") for layout in layouts.values()
    }
    if len(layout_values) != 1:
        raise ReleaseGateError("launcher race install layouts differ")
    _validate_installed_launcher_wave(waves["before"], "before", before, after)
    _validate_installed_launcher_wave(waves["race"], "race", before, after)
    _validate_installed_launcher_wave(
        waves["after_concurrent"],
        "after_concurrent",
        before,
        after,
    )
    _validate_installed_launcher_wave(waves["after"], "after", before, after)
    return {
        "active_runtime_identity": after,
        "launcher_layout_sha256": next(iter(layout_values)),
    }


def _validate_installed_launcher_wave(
    value: Any,
    phase: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    entries = value
    surfaces = (
        "launcher_guard",
        "update_guard",
        "gui",
        "cli",
        "quick_start",
        "run_app_bat",
        "run_quickstart_bat",
    )
    expected_pairs = [
        (label, surface)
        for label in ("source-default", "packaged-default")
        for surface in surfaces
    ]
    if not isinstance(entries, list) or len(entries) != len(expected_pairs):
        raise ReleaseGateError(f"launcher {phase} installed wave is incomplete")
    selected_count = 0
    for entry_value, expected_pair in zip(entries, expected_pairs):
        entry = _object(entry_value, f"launcher {phase} wave entry")
        _exact_keys(
            entry,
            {
                "label",
                "surface",
                "exit_code",
                "duration_ns",
                "output_sha256",
                "disposition",
                "runtime_identity",
            },
            f"launcher {phase} wave entry",
        )
        pair = (entry["label"], entry["surface"])
        if (
            pair != expected_pair
            or not isinstance(entry["exit_code"], int)
            or isinstance(entry["exit_code"], bool)
            or not isinstance(entry["duration_ns"], int)
            or isinstance(entry["duration_ns"], bool)
            or entry["duration_ns"] <= 0
        ):
            raise ReleaseGateError(f"launcher {phase} wave topology is invalid")
        _require_digest(entry["output_sha256"], f"launcher {phase} output")
        if phase == "before":
            selected = entry["surface"] != "update_guard"
            expected_identity = before if selected else None
        elif phase == "race":
            selected = False
            expected_identity = None
        elif phase == "after":
            selected = True
            expected_identity = after
        elif phase == "after_concurrent":
            selected = entry["disposition"] == "selected"
            expected_identity = after if selected else None
        else:
            raise ReleaseGateError("launcher wave phase is invalid")
        selected_count += int(selected)
        expected_disposition = "selected" if selected else "blocked"
        if (
            entry["disposition"] != expected_disposition
            or (entry["exit_code"] == 0) is not selected
            or canonical_json(entry["runtime_identity"])
            != canonical_json(expected_identity)
        ):
            raise ReleaseGateError(f"launcher {phase} wave disposition is invalid")
    if phase == "after_concurrent" and selected_count == 0:
        raise ReleaseGateError(
            "launcher concurrent wave did not select the published runtime"
        )


def _launcher_runtime_tuple_sha256(identity: Mapping[str, Any]) -> str:
    values = [
        identity[field]
        for field in (
            "mode",
            "active_snapshot_id",
            "publication_generation",
            "write_epoch",
            "v1_fallback_open",
            "degraded",
            "write_enabled",
        )
    ]
    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()


def _validate_compatibility(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "compatibility evidence")
    required = {
        "schema_version",
        "kind",
        "passed",
        "approved",
        "release_eligible",
        "compatibility_exception_required",
        "runtime",
        "snapshot_descriptors",
        "structural_delta",
        "retrieval_quality",
        "proposed_exception",
    }
    _exact_keys(obj, required, "compatibility evidence")
    runtime = _object(obj["runtime"], "compatibility runtime")
    _exact_keys(
        runtime,
        {
            "active_snapshot_id",
            "predecessor_snapshot_id",
            "publication_generation",
            "write_epoch",
        },
        "compatibility runtime",
    )
    descriptors = _object(
        obj["snapshot_descriptors"],
        "compatibility snapshot descriptors",
    )
    _exact_keys(
        descriptors,
        {"active", "predecessor"},
        "compatibility snapshot descriptors",
    )
    active_descriptor = _validate_compatibility_descriptor(
        descriptors["active"],
        "active compatibility descriptor",
    )
    predecessor_descriptor = _validate_compatibility_descriptor(
        descriptors["predecessor"],
        "predecessor compatibility descriptor",
    )
    structural = _object(obj["structural_delta"], "compatibility structural delta")
    _exact_keys(
        structural,
        {
            "active_report_count",
            "predecessor_report_count",
            "common_report_count",
            "new_report_count",
            "new_report_uids",
            "missing_report_count",
            "missing_report_uids",
            "unique_chunk_content_loss",
            "removed_duplicate_occurrences",
            "multiplicity_delta_report_count",
            "multiplicity_delta_reports",
            "parent_order_delta_report_count",
            "parent_order_delta_reports",
        },
        "compatibility structural delta",
    )
    quality = _object(obj["retrieval_quality"], "compatibility quality")
    _exact_keys(
        quality,
        {
            "k",
            "predecessor_vector_queries",
            "expected_content_rank_one",
            "expected_content_within_k",
            "source_top_one_equal",
            "citation_complete_top_one",
            "minimum_source_set_recall_at_k",
            "mean_source_set_recall_at_k",
            "gate_d_query_passed",
            "gate_d_query_spec_sha256",
            "gate_d_expected_report_uid",
        },
        "compatibility quality",
    )
    proposed = _object(obj["proposed_exception"], "compatibility exception")
    _exact_keys(
        proposed,
        {
            "reason_codes",
            "unique_content_loss_allowed",
            "citation_loss_allowed",
            "approval_required",
        },
        "compatibility exception",
    )
    reason_codes = [
        "collapse_exact_v1_duplicate_occurrences",
        "canonicalize_parent_order_from_source_rebuild",
    ]
    seed = context["conversion"]["validation"]
    race = context["successor_race"]["validation"]
    runtime_integers = (
        runtime["publication_generation"],
        runtime["write_epoch"],
    )
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_successor_compatibility_exception_evidence"
        or obj["passed"] is not True
        or obj["approved"] is not False
        or obj["release_eligible"] is not False
        or obj["compatibility_exception_required"] is not True
        or any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in runtime_integers
        )
        or runtime["active_snapshot_id"] != race["active_snapshot_id"]
        or runtime["predecessor_snapshot_id"] != race["predecessor_snapshot_id"]
        or runtime["publication_generation"] != race["publication_generation"]
        or runtime["write_epoch"] != race["write_epoch"]
        or active_descriptor["snapshot_id"] != runtime["active_snapshot_id"]
        or predecessor_descriptor["snapshot_id"]
        != runtime["predecessor_snapshot_id"]
        or predecessor_descriptor["snapshot_id"] != seed["snapshot_id"]
        or predecessor_descriptor["sha256"] != seed["snapshot_sha256"]
        or predecessor_descriptor["ntotal"] != seed["chunk_count"]
        or active_descriptor["snapshot_id"] != race["active_snapshot_id"]
        or active_descriptor["ntotal"] != race["candidate_chunk_count"]
        or active_descriptor["dimension"] != predecessor_descriptor["dimension"]
        or active_descriptor["metric"] != predecessor_descriptor["metric"]
        or proposed["reason_codes"] != reason_codes
        or proposed["unique_content_loss_allowed"] != 0
        or isinstance(proposed["unique_content_loss_allowed"], bool)
        or proposed["citation_loss_allowed"] != 0
        or isinstance(proposed["citation_loss_allowed"], bool)
        or proposed["approval_required"] is not True
    ):
        raise ReleaseGateError("successor compatibility gate is invalid")

    structural_counts = {
        field: structural[field]
        for field in (
            "active_report_count",
            "predecessor_report_count",
            "common_report_count",
            "new_report_count",
            "missing_report_count",
            "unique_chunk_content_loss",
            "removed_duplicate_occurrences",
            "multiplicity_delta_report_count",
            "parent_order_delta_report_count",
        )
    }
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in structural_counts.values()
    ):
        raise ReleaseGateError("compatibility structural counts are invalid")
    new_uids = _validate_sorted_digest_list(
        structural["new_report_uids"],
        "compatibility new report UIDs",
    )
    missing_uids = _validate_sorted_digest_list(
        structural["missing_report_uids"],
        "compatibility missing report UIDs",
    )
    multiplicity = _validate_compatibility_deltas(
        structural["multiplicity_delta_reports"],
        "compatibility multiplicity deltas",
        require_equal_counts=False,
    )
    parent_order = _validate_compatibility_deltas(
        structural["parent_order_delta_reports"],
        "compatibility parent-order deltas",
        require_equal_counts=True,
    )
    removed_occurrences = sum(
        item["predecessor_chunk_count"] - item["active_chunk_count"]
        for item in multiplicity
    )
    multiplicity_uids = [item["report_uid"] for item in multiplicity]
    parent_order_uids = [item["report_uid"] for item in parent_order]
    if (
        structural["active_report_count"] != race["candidate_report_count"]
        or structural["predecessor_report_count"] != seed["report_count"]
        or structural["common_report_count"] + structural["new_report_count"]
        != structural["active_report_count"]
        or structural["common_report_count"] + structural["missing_report_count"]
        != structural["predecessor_report_count"]
        or structural["new_report_count"] != len(new_uids)
        or structural["missing_report_count"] != len(missing_uids)
        or structural["missing_report_count"] != 0
        or missing_uids
        or structural["unique_chunk_content_loss"] != 0
        or structural["multiplicity_delta_report_count"] != len(multiplicity)
        or structural["parent_order_delta_report_count"] != len(parent_order)
        or not multiplicity
        or not parent_order
        or multiplicity_uids != sorted(set(multiplicity_uids))
        or parent_order_uids != sorted(set(parent_order_uids))
        or set(multiplicity_uids) & set(parent_order_uids)
        or set(new_uids) & (set(multiplicity_uids) | set(parent_order_uids))
        or structural["removed_duplicate_occurrences"] != removed_occurrences
        or removed_occurrences <= 0
    ):
        raise ReleaseGateError("compatibility structural delta is invalid")

    count_fields = (
        "k",
        "predecessor_vector_queries",
        "expected_content_rank_one",
        "expected_content_within_k",
        "source_top_one_equal",
        "citation_complete_top_one",
    )
    if any(
        not isinstance(quality[field], int)
        or isinstance(quality[field], bool)
        or quality[field] <= 0
        for field in count_fields
    ):
        raise ReleaseGateError("compatibility quality counts are invalid")
    query_count = quality["predecessor_vector_queries"]
    minimum_recall = quality["minimum_source_set_recall_at_k"]
    mean_recall = quality["mean_source_set_recall_at_k"]
    _require_digest(
        quality["gate_d_query_spec_sha256"],
        "compatibility Gate D query",
    )
    _require_digest(
        quality["gate_d_expected_report_uid"],
        "compatibility Gate D expected report",
    )
    if (
        query_count != predecessor_descriptor["ntotal"]
        or not 0 < quality["k"] <= min(
            active_descriptor["ntotal"],
            predecessor_descriptor["ntotal"],
        )
        or any(
            quality[field] != query_count
            for field in (
                "expected_content_rank_one",
                "expected_content_within_k",
                "source_top_one_equal",
                "citation_complete_top_one",
            )
        )
        or not isinstance(minimum_recall, (int, float))
        or isinstance(minimum_recall, bool)
        or not math.isfinite(float(minimum_recall))
        or not isinstance(mean_recall, (int, float))
        or isinstance(mean_recall, bool)
        or not math.isfinite(float(mean_recall))
        or not 0.0 <= float(minimum_recall) <= float(mean_recall) <= 1.0
        or quality["gate_d_query_passed"] is not True
        or quality["gate_d_expected_report_uid"] not in new_uids
    ):
        raise ReleaseGateError("compatibility retrieval quality is invalid")
    return {
        "kind": obj["kind"],
        "active_snapshot_id": runtime["active_snapshot_id"],
        "predecessor_snapshot_id": runtime["predecessor_snapshot_id"],
        "publication_generation": runtime["publication_generation"],
        "write_epoch": runtime["write_epoch"],
        "active_descriptor": active_descriptor,
        "predecessor_descriptor": predecessor_descriptor,
        "predecessor_vector_queries": query_count,
        "expected_content_rank_one": quality["expected_content_rank_one"],
        "source_top_one_equal": quality["source_top_one_equal"],
        "citation_complete_top_one": quality["citation_complete_top_one"],
        "gate_d_query_spec_sha256": quality["gate_d_query_spec_sha256"],
        "gate_d_expected_report_uid": quality["gate_d_expected_report_uid"],
        "approved_exception_reason_codes": reason_codes,
        "multiplicity_delta_report_uids": multiplicity_uids,
        "parent_order_delta_report_uids": parent_order_uids,
    }


def _validate_compatibility_descriptor(value: Any, label: str) -> dict[str, Any]:
    descriptor = _object(value, label)
    _exact_keys(
        descriptor,
        {"snapshot_id", "sha256", "size_bytes", "dimension", "metric", "ntotal"},
        label,
    )
    _require_digest(descriptor["snapshot_id"], f"{label} snapshot ID")
    _require_digest(descriptor["sha256"], f"{label} snapshot")
    if (
        any(
            not isinstance(descriptor[field], int)
            or isinstance(descriptor[field], bool)
            or descriptor[field] <= 0
            for field in ("size_bytes", "dimension", "ntotal")
        )
        or descriptor["metric"] not in {"l2", "inner_product"}
    ):
        raise ReleaseGateError(f"{label} is invalid")
    return dict(descriptor)


def _validate_sorted_digest_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ReleaseGateError(f"{label} are invalid")
    for item in value:
        _require_digest(item, label)
    if value != sorted(set(value)):
        raise ReleaseGateError(f"{label} are invalid")
    return list(value)


def _validate_compatibility_deltas(
    value: Any,
    label: str,
    *,
    require_equal_counts: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ReleaseGateError(f"{label} are invalid")
    result: list[dict[str, Any]] = []
    fields = {
        "report_uid",
        "canonical_relative_path",
        "predecessor_parent_count",
        "active_parent_count",
        "predecessor_chunk_count",
        "active_chunk_count",
    }
    for raw in value:
        item = _object(raw, label)
        _exact_keys(item, fields, label)
        _require_digest(item["report_uid"], f"{label} report UID")
        counts = [
            item[field]
            for field in (
                "predecessor_parent_count",
                "active_parent_count",
                "predecessor_chunk_count",
                "active_chunk_count",
            )
        ]
        if (
            not isinstance(item["canonical_relative_path"], str)
            or not item["canonical_relative_path"].strip()
            or any(
                not isinstance(count, int)
                or isinstance(count, bool)
                or count <= 0
                for count in counts
            )
        ):
            raise ReleaseGateError(f"{label} are invalid")
        if require_equal_counts:
            valid_counts = (
                item["predecessor_parent_count"] == item["active_parent_count"]
                and item["predecessor_chunk_count"] == item["active_chunk_count"]
            )
        else:
            valid_counts = (
                item["predecessor_parent_count"] >= item["active_parent_count"]
                and item["predecessor_chunk_count"] > item["active_chunk_count"]
            )
        if not valid_counts:
            raise ReleaseGateError(f"{label} are invalid")
        result.append(dict(item))
    return result


def _validate_compatibility_approval(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "compatibility approval")
    required = {
        "schema_version",
        "kind",
        "reviewed_at_utc",
        "reviewer_role",
        "verdict",
        "approved",
        "release_eligible",
        "open_p0_count",
        "open_p1_count",
        "source_evidence",
        "approved_exceptions",
        "multiplicity_delta_report_uids",
        "parent_order_delta_report_uids",
        "required_invariants",
        "directives",
    }
    _exact_keys(obj, required, "compatibility approval")
    source = _object(obj["source_evidence"], "compatibility approval source")
    _exact_keys(
        source,
        {"relative_path", "sha256", "active_snapshot_id", "predecessor_snapshot_id"},
        "compatibility approval source",
    )
    compatibility = context["compatibility"]
    expected = compatibility["validation"]
    approved_exceptions = obj["approved_exceptions"]
    multiplicity_uids = obj["multiplicity_delta_report_uids"]
    parent_order_uids = obj["parent_order_delta_report_uids"]
    invariants = _object(
        obj["required_invariants"],
        "compatibility approval invariants",
    )
    expected_invariants = {
        "unique_chunk_content_loss": 0,
        "citation_loss": 0,
        "predecessor_vector_queries": expected["predecessor_vector_queries"],
        "expected_content_rank_one": expected["expected_content_rank_one"],
        "source_top_one_equal": expected["source_top_one_equal"],
        "citation_complete_top_one": expected["citation_complete_top_one"],
        "gate_d_query_passed": True,
    }
    _exact_keys(
        invariants,
        set(expected_invariants),
        "compatibility approval invariants",
    )
    reviewed_at = _parse_utc_timestamp(
        obj["reviewed_at_utc"],
        "compatibility approval timestamp",
    )
    relative_path = source.get("relative_path")
    source_path = Path(relative_path) if isinstance(relative_path, str) else None
    source_path_is_safe = (
        source_path is not None
        and relative_path == source_path.as_posix()
        and not source_path.is_absolute()
        and ".." not in source_path.parts
        and relative_path == compatibility["name"]
    )
    _require_digest(source.get("sha256"), "compatibility approval source")
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_successor_compatibility_architect_approval"
        or obj["reviewer_role"] != "independent_architect"
        or obj["verdict"] != "approve"
        or obj["approved"] is not True
        or obj["release_eligible"] is not False
        or obj["open_p0_count"] != 0
        or obj["open_p1_count"] != 0
        or not source_path_is_safe
        or source.get("sha256") != compatibility["sha256"]
        or source.get("active_snapshot_id") != expected["active_snapshot_id"]
        or source.get("predecessor_snapshot_id") != expected["predecessor_snapshot_id"]
        or approved_exceptions != expected["approved_exception_reason_codes"]
        or multiplicity_uids != expected["multiplicity_delta_report_uids"]
        or parent_order_uids != expected["parent_order_delta_report_uids"]
        or invariants != expected_invariants
        or any(
            not isinstance(value, int) or isinstance(value, bool)
            for key, value in invariants.items()
            if key != "gate_d_query_passed"
        )
        or invariants["gate_d_query_passed"] is not True
        or not isinstance(obj["directives"], list)
        or len(obj["directives"]) != 3
        or len(set(obj["directives"])) != 3
        or any(
            not isinstance(item, str) or not item.strip()
            for item in obj["directives"]
        )
    ):
        raise ReleaseGateError("compatibility approval is not bound to its evidence")
    return {
        "kind": obj["kind"],
        "approved": True,
        "reviewed_at_utc": reviewed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open_p0_count": 0,
        "open_p1_count": 0,
    }


def _validate_performance(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
    *,
    expected_pair: str,
) -> dict[str, Any]:
    obj = _object(value, "performance evidence")
    _exact_keys(obj, {"schema_version", "kind", "raw", "analysis"}, "performance evidence")
    raw = _object(obj["raw"], "performance raw evidence")
    sealed = dict(_object(obj["analysis"], "performance analysis"))
    protocol = _object(sealed.get("protocol"), "performance protocol")
    bootstrap_resamples = protocol.get("bootstrap_resamples")
    bootstrap_seed = protocol.get("bootstrap_seed")
    if (
        not isinstance(bootstrap_resamples, int)
        or isinstance(bootstrap_resamples, bool)
        or bootstrap_resamples <= 0
        or not isinstance(bootstrap_seed, int)
        or isinstance(bootstrap_seed, bool)
    ):
        raise ReleaseGateError("performance bootstrap protocol is invalid")
    try:
        recomputed = analyze_benchmark(
            raw,
            bootstrap_resamples=bootstrap_resamples,
            bootstrap_seed=bootstrap_seed,
        )
    except ValueError as exc:
        raise ReleaseGateError("performance raw evidence is invalid") from exc
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_retrieval_performance_evidence"
        or sealed.get("schema_version") != 1
        or sealed.get("kind") != "v2_retrieval_performance_analysis"
        or canonical_json(sealed) != canonical_json(recomputed)
        or sealed.get("passed") is not True
    ):
        raise ReleaseGateError("performance analysis does not reproduce")
    environment = _object(raw.get("environment"), "performance environment")
    provenance = _object(raw.get("provenance"), "performance provenance")
    pair = sealed["benchmark_pair"]
    expected_profile = (
        "epoch_zero"
        if expected_pair == "v1_compatibility_vs_native"
        else "successor_release"
    )
    expected_factory = (
        "src.migrations.v2.validation.copied_install_benchmark:create_factory"
        if expected_profile == "epoch_zero"
        else "src.migrations.v2.validation.copied_install_benchmark:create_successor_factory"
    )
    minimum_samples = 200 if expected_profile == "epoch_zero" else 4_000
    minimum_bootstrap = 500 if expected_profile == "epoch_zero" else 10_000
    expected_process_policy = "minimum" if expected_profile == "epoch_zero" else "exact"
    if (
        pair != expected_pair
        or raw.get("protocol_profile") != expected_profile
        or protocol.get("profile") != expected_profile
        or protocol.get("required_workloads") != list(REQUIRED_WORKLOADS)
        or protocol.get("minimum_processes") != 3
        or protocol.get("process_count_policy") != expected_process_policy
        or protocol.get("minimum_warmups") != 10
        or protocol.get("minimum_timed_samples_per_process_workload")
        != minimum_samples
        or protocol.get("minimum_fixed_queries_per_workload") != 30
        or protocol.get("minimum_bootstrap_resamples") != minimum_bootstrap
        or bootstrap_resamples < minimum_bootstrap
        or provenance.get("factory_entrypoint") != expected_factory
        or provenance.get("adapter_callable") != expected_factory
    ):
        raise ReleaseGateError("performance benchmark targets the wrong pair")
    try:
        verify_current_benchmark_provenance(
            provenance,
            runner_path=(
                REPOSITORY_ROOT
                / "scripts"
                / "migrations"
                / "v2"
                / "run_v2_retrieval_benchmark.py"
            ),
        )
    except BenchmarkProvenanceError as exc:
        raise ReleaseGateError(
            "performance factory or provenance does not match current code"
        ) from exc
    shared_provenance = {
        field: provenance[field]
        for field in (
            "runner_entrypoint",
            "runner_module_sha256",
            "runtime_code_layout_sha256",
            "runtime_code_file_count",
            "layout_algorithm",
            "interpreter",
        )
    }

    common_environment_fields = {
        "os",
        "os_release",
        "python_version",
        "faiss_version",
        "numpy_version",
        "cache_state",
        "reranker",
        "query_input_sha256",
        "dimension",
        "metric",
        "k",
    }
    epoch_fields = common_environment_fields | {
        "active_snapshot_id",
        "active_snapshot_sha256",
        "ntotal",
    }
    successor_fields = common_environment_fields | {
        "benchmark_pair",
        "catalog_policy",
        "baseline_snapshot_id",
        "baseline_snapshot_sha256",
        "baseline_ntotal",
        "candidate_snapshot_id",
        "candidate_snapshot_sha256",
        "candidate_ntotal",
        "write_epoch",
        "v1_fallback_open",
    }
    _exact_keys(
        environment,
        epoch_fields if expected_profile == "epoch_zero" else successor_fields,
        "performance environment",
    )
    _require_digest(environment["query_input_sha256"], "performance query input")
    numeric_fields = ["dimension", "k"]
    numeric_fields.extend(
        ["ntotal"]
        if expected_profile == "epoch_zero"
        else ["baseline_ntotal", "candidate_ntotal", "write_epoch"]
    )
    if (
        any(
            not isinstance(environment[field], str)
            or not environment[field].strip()
            for field in (
                "os",
                "os_release",
                "python_version",
                "faiss_version",
                "numpy_version",
                "cache_state",
                "reranker",
                "metric",
            )
        )
        or any(
            not isinstance(environment[field], int)
            or isinstance(environment[field], bool)
            or environment[field] <= 0
            for field in numeric_fields
        )
        or environment["os"] != "windows"
        or environment["python_version"]
        != provenance["interpreter"]["version"]
        or environment["cache_state"] != "warm"
        or environment["reranker"] != "disabled-reader-parity"
        or environment["metric"] not in {"l2", "inner_product"}
    ):
        raise ReleaseGateError("performance environment is invalid")

    if expected_pair == "v1_compatibility_vs_native":
        seed = context["conversion"]["validation"]
        parity = context["reader_parity"]["validation"]
        for field in ("active_snapshot_id", "active_snapshot_sha256"):
            _require_digest(environment[field], f"epoch-zero performance {field}")
        if (
            environment["active_snapshot_id"] != seed["snapshot_id"]
            or environment["active_snapshot_sha256"] != seed["snapshot_sha256"]
            or environment["ntotal"] != seed["chunk_count"]
            or environment["active_snapshot_id"] != parity["snapshot_id"]
            or environment["active_snapshot_sha256"] != parity["snapshot_sha256"]
            or environment["dimension"] != parity["dimension"]
            or environment["metric"] != parity["metric"]
            or environment["ntotal"] != parity["ntotal"]
            or not 0 < environment["k"] <= environment["ntotal"]
        ):
            raise ReleaseGateError("epoch-zero benchmark does not target the converted seed")
    else:
        compatibility = context["compatibility"]["validation"]
        active = compatibility["active_descriptor"]
        predecessor = compatibility["predecessor_descriptor"]
        for field in (
            "baseline_snapshot_id",
            "baseline_snapshot_sha256",
            "candidate_snapshot_id",
            "candidate_snapshot_sha256",
        ):
            _require_digest(environment[field], f"successor performance {field}")
        if (
            environment["catalog_policy"]
            != "shared_checkpointed_catalog_clone_pinned_revisions"
        ):
            raise ReleaseGateError(
                "successor performance catalog policy is invalid"
            )
        if (
            environment["benchmark_pair"] != expected_pair
            or environment["baseline_snapshot_id"] != predecessor["snapshot_id"]
            or environment["baseline_snapshot_sha256"] != predecessor["sha256"]
            or environment["baseline_ntotal"] != predecessor["ntotal"]
            or environment["candidate_snapshot_id"] != active["snapshot_id"]
            or environment["candidate_snapshot_sha256"] != active["sha256"]
            or environment["candidate_ntotal"] != active["ntotal"]
            or environment["dimension"] != active["dimension"]
            or environment["metric"] != active["metric"]
            or environment["write_epoch"] != compatibility["write_epoch"]
            or environment["v1_fallback_open"] is not False
            or not 0 < environment["k"] <= min(
                environment["baseline_ntotal"],
                environment["candidate_ntotal"],
            )
        ):
            raise ReleaseGateError("successor benchmark does not target the approved pair")
        epoch = context["epoch_zero_performance"]["validation"]
        for field in (
            "os",
            "os_release",
            "python_version",
            "faiss_version",
            "numpy_version",
            "cache_state",
            "reranker",
            "dimension",
            "metric",
        ):
            if environment[field] != epoch["environment"][field]:
                raise ReleaseGateError("performance benchmark environments differ")
        if provenance["adapter_module_sha256"] != epoch["adapter_module_sha256"]:
            raise ReleaseGateError("performance benchmark adapter modules differ")
        if canonical_json(shared_provenance) != canonical_json(
            epoch["shared_provenance"]
        ):
            raise ReleaseGateError("performance benchmark provenance differs")

    return {
        "kind": obj["kind"],
        "benchmark_pair": pair,
        "protocol_profile": expected_profile,
        "factory_entrypoint": expected_factory,
        "adapter_module_sha256": provenance["adapter_module_sha256"],
        "shared_provenance": shared_provenance,
        "environment": dict(environment),
        "passed": True,
    }


def _validate_epoch_zero_performance(value: Any, path: Path, context: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_performance(
        value,
        path,
        context,
        expected_pair="v1_compatibility_vs_native",
    )


def _validate_successor_performance(value: Any, path: Path, context: Mapping[str, Any]) -> dict[str, Any]:
    return _validate_performance(
        value,
        path,
        context,
        expected_pair="native_predecessor_vs_native_successor",
    )


def _validate_query(
    value: Any,
    path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "semantic query")
    required = {
        "schema_version",
        "kind",
        "query_id",
        "query_text",
        "vector",
        "k",
        "expected_report_uid",
        "expected_citation",
        "scopes",
        "embedding_attestation",
    }
    _exact_keys(obj, required, "semantic query")
    vector = obj["vector"]
    attestation = _object(obj["embedding_attestation"], "query attestation")
    citation = _object(obj["expected_citation"], "expected citation")
    scopes = _object(obj["scopes"], "query scopes")
    citation_fields = {
        "canonical_relative_path",
        "report_type",
        "report_date",
        "target_name",
        "title",
        "broker",
    }
    _exact_keys(citation, citation_fields, "expected citation")
    _exact_keys(
        attestation,
        {
            "provider",
            "model",
            "input_type",
            "provider_calls",
            "query_text_sha256",
            "vector_sha256",
        },
        "query attestation",
    )
    active_descriptor = context["compatibility"]["validation"]["active_descriptor"]
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_release_semantic_query"
        or not isinstance(obj["query_id"], str)
        or not obj["query_id"].strip()
        or not isinstance(obj["query_text"], str)
        or not obj["query_text"].strip()
        or not isinstance(vector, list)
        or not vector
        or len(vector) != active_descriptor["dimension"]
        or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            for item in vector
        )
        or not isinstance(obj["k"], int)
        or isinstance(obj["k"], bool)
        or not 0 < obj["k"] <= active_descriptor["ntotal"]
        or set(scopes) != set(REQUIRED_WORKLOADS)
        or scopes["unfiltered"] is not None
        or not isinstance(scopes["empty"], Mapping)
        or scopes["empty"].get("empty") is not True
        or any(
            not isinstance(scopes[name], Mapping) or not scopes[name]
            for name in REQUIRED_WORKLOADS
            if name not in {"unfiltered", "empty"}
        )
        or any(
            not isinstance(citation[field], str) or not citation[field].strip()
            for field in citation_fields
        )
    ):
        raise ReleaseGateError("semantic query vector, workload, or contract is invalid")
    _require_digest(obj["expected_report_uid"], "expected report UID")
    query_hash = hashlib.sha256(obj["query_text"].encode("utf-8")).hexdigest()
    vector_hash = hashlib.sha256(_float32_bytes(vector)).hexdigest()
    if (
        attestation.get("provider") != "openrouter"
        or attestation.get("model") != "baai/bge-m3"
        or attestation.get("input_type") != "search_query"
        or attestation.get("provider_calls") != 1
        or isinstance(attestation.get("provider_calls"), bool)
        or attestation.get("query_text_sha256") != query_hash
        or attestation.get("vector_sha256") != vector_hash
    ):
        raise ReleaseGateError("semantic query attestation is invalid")
    compatibility = context["compatibility"]["validation"]
    query_spec_sha256 = _sha256_file(path)
    if (
        query_spec_sha256 != compatibility["gate_d_query_spec_sha256"]
        or obj["expected_report_uid"]
        != compatibility["gate_d_expected_report_uid"]
    ):
        raise ReleaseGateError(
            "semantic query is not bound to the compatibility evidence"
        )
    citation_hash = hashlib.sha256(
        canonical_json(citation).encode("utf-8")
    ).hexdigest()
    attestation_hash = hashlib.sha256(
        canonical_json(attestation).encode("utf-8")
    ).hexdigest()
    return {
        "kind": obj["kind"],
        "query_id": obj["query_id"],
        "query_spec_sha256": query_spec_sha256,
        "expected_report_uid": obj["expected_report_uid"],
        "query_text_sha256": query_hash,
        "vector_sha256": vector_hash,
        "vector_dimension": len(vector),
        "model": attestation["model"],
        "expected_citation_sha256": citation_hash,
        "attestation_sha256": attestation_hash,
    }


def _validate_transition(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "transition evidence")
    try:
        transition_summary = validate_release_transition_evidence(obj)
    except ReleaseTransitionError as exc:
        raise ReleaseGateError("transition evidence is invalid") from exc
    final = transition_summary["final_runtime_identity"]
    started_at = _parse_utc_timestamp(obj.get("started_at"), "transition start")
    completed_at = _parse_utc_timestamp(
        obj.get("completed_at"),
        "transition completion",
    )
    if completed_at < started_at:
        raise ReleaseGateError("transition timestamps are invalid")
    query_entry = context["query_spec"]
    query = query_entry["validation"]
    compatibility = context["compatibility"]["validation"]
    initial = _object(obj.get("initial"), "transition initial runtime")
    expected_initial = {
        "active_snapshot_id": compatibility["active_snapshot_id"],
        "predecessor_snapshot_id": compatibility["predecessor_snapshot_id"],
        "publication_generation": compatibility["publication_generation"],
        "write_epoch": compatibility["write_epoch"],
        "v1_fallback_open": False,
        "degraded": False,
        "write_enabled": True,
    }
    copy_proof = _object(obj.get("copy_proof"), "transition copy proof")
    initial_hashes = _object(
        copy_proof.get("initial_snapshot_sha256"),
        "transition initial snapshot hashes",
    )
    expected_initial_hashes = {
        "active": compatibility["active_descriptor"]["sha256"],
        "predecessor": compatibility["predecessor_descriptor"]["sha256"],
    }
    if (
        canonical_json(initial) != canonical_json(expected_initial)
        or initial_hashes != expected_initial_hashes
    ):
        raise ReleaseGateError("transition initial snapshot pair is invalid")
    if copy_proof.get("query_spec_sha256") != query_entry["sha256"]:
        raise ReleaseGateError("transition evidence is not bound to the query")
    gate_d = _object(obj.get("gate_d_search"), "transition Gate D")
    generation = _object(
        gate_d.get("query_generation"),
        "transition Gate D generation",
    )
    expected_gate_d = {
        "query_id": query["query_id"],
        "query_text_sha256": query["query_text_sha256"],
        "query_vector_sha256": query["vector_sha256"],
        "query_spec_sha256": query_entry["sha256"],
        "expected_report_uid": query["expected_report_uid"],
        "top_report_uid": query["expected_report_uid"],
        "top_rank": 1,
        "citation_complete": True,
        "citation_sha256": query["expected_citation_sha256"],
    }
    if any(gate_d.get(field) != expected for field, expected in expected_gate_d.items()):
        raise ReleaseGateError("transition Gate D query binding is invalid")
    if (
        generation.get("provider") != "openrouter"
        or generation.get("model") != query["model"]
        or generation.get("input_type") != "search_query"
        or generation.get("provider_calls") != 1
        or isinstance(generation.get("provider_calls"), bool)
        or generation.get("attestation_sha256") != query["attestation_sha256"]
    ):
        raise ReleaseGateError("transition Gate D attestation is invalid")
    return {
        "kind": obj["kind"],
        "run_id": transition_summary["run_id"],
        "initial_runtime_identity": expected_initial,
        "final_runtime_identity": final,
        "query_spec_sha256": transition_summary["query_spec_sha256"],
        "protected_tree_sha256_after": transition_summary[
            "protected_tree_sha256_after"
        ],
        "source_tree_sha256_after": transition_summary[
            "source_tree_sha256_after"
        ],
    }


def _validate_installed_validation(
    value: Any,
    _path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "installed validation evidence")
    query_entry = context["query_spec"]
    transition_entry = context["transition"]
    try:
        return validate_installed_validation_evidence(
            obj,
            query=query_entry["validation"],
            query_spec_sha256=query_entry["sha256"],
            transition=transition_entry["validation"],
            transition_sha256=transition_entry["sha256"],
            launcher_layout_sha256=context["launcher_matrix"]["validation"][
                "launcher_layout_sha256"
            ],
        )
    except InstalledValidationError as exc:
        raise ReleaseGateError("installed validation evidence is invalid") from exc


def _parse_utc_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReleaseGateError(f"{label} is not a UTC timestamp")
    try:
        timestamp = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ReleaseGateError(f"{label} is not a UTC timestamp") from exc
    return timestamp


def _validate_pytest_attestation(
    value: Any,
    _path: Path,
    _context: Mapping[str, Any],
) -> dict[str, Any]:
    obj = _object(value, "pytest attestation")
    _exact_keys(
        obj,
        {
            "schema_version",
            "kind",
            "status",
            "protocol",
            "commands",
            "interpreter",
            "layouts",
            "collection",
            "junit",
        },
        "pytest attestation",
    )
    protocol = _object(obj["protocol"], "pytest attestation protocol")
    _exact_keys(
        protocol,
        {
            "collection_exit_code",
            "execution_exit_code",
            "selection_args_allowed",
            "test_target",
        },
        "pytest attestation protocol",
    )
    commands = _object(obj["commands"], "pytest attestation commands")
    _exact_keys(
        commands,
        {
            "working_directory",
            "collection_argv",
            "execution_argv",
            "environment",
        },
        "pytest attestation commands",
    )
    interpreter = _object(
        obj["interpreter"],
        "pytest attestation interpreter",
    )
    _exact_keys(
        interpreter,
        {
            "executable",
            "executable_sha256",
            "implementation",
            "python_version",
            "pytest_version",
        },
        "pytest attestation interpreter",
    )
    layouts = _object(obj["layouts"], "pytest attestation layouts")
    _exact_keys(
        layouts,
        {
            "algorithm",
            "test_file_count",
            "test_layout_sha256",
            "source_file_count",
            "source_layout_sha256",
        },
        "pytest attestation layouts",
    )
    collection = _object(
        obj["collection"],
        "pytest attestation collection",
    )
    _exact_keys(
        collection,
        {"count", "nodeids", "nodeids_sha256"},
        "pytest attestation collection",
    )
    junit = _object(obj["junit"], "pytest attestation JUnit")
    _exact_keys(
        junit,
        {
            "suite_count",
            "testcase_count",
            "failures",
            "errors",
            "skipped",
            "output_name",
            "sha256",
        },
        "pytest attestation JUnit",
    )

    executable = _safe_file(Path(sys.executable), "pytest interpreter")
    expected_executable = str(executable)
    expected_collection = [
        expected_executable,
        *run_release_pytest._COLLECTION_ARGUMENTS,
    ]
    expected_execution_prefix = [
        expected_executable,
        *run_release_pytest._EXECUTION_ARGUMENTS,
    ]
    execution_argv = commands["execution_argv"]
    if (
        not isinstance(execution_argv, list)
        or len(execution_argv) != len(expected_execution_prefix) + 1
        or execution_argv[:-1] != expected_execution_prefix
        or not isinstance(execution_argv[-1], str)
        or not execution_argv[-1].startswith("--junitxml=")
    ):
        raise ReleaseGateError("pytest execution command permits partial selection")
    temporary_value = execution_argv[-1].partition("=")[2]
    temporary_path = Path(temporary_value)
    output_name = junit["output_name"]
    temporary_pattern = (
        rf"^\.{re.escape(output_name)}\.[0-9a-f]{{16}}\.tmp$"
        if isinstance(output_name, str)
        else ""
    )
    if (
        not temporary_path.is_absolute()
        or not isinstance(output_name, str)
        or not output_name
        or Path(output_name).name != output_name
        or re.fullmatch(temporary_pattern, temporary_path.name) is None
    ):
        raise ReleaseGateError("pytest JUnit command output is invalid")
    try:
        temporary_parent = temporary_path.parent.resolve(strict=True)
    except OSError as exc:
        raise ReleaseGateError("pytest JUnit command output parent is invalid") from exc

    try:
        current_layouts = run_release_pytest._capture_release_layouts(
            REPOSITORY_ROOT.resolve(strict=True)
        )
    except run_release_pytest.ReleasePytestError as exc:
        raise ReleaseGateError("pytest release layout cannot be verified") from exc
    expected_environment = run_release_pytest._pytest_environment()[1]
    _require_digest(
        interpreter["executable_sha256"],
        "pytest interpreter executable",
    )
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_release_pytest_attestation"
        or obj["status"] != "passed"
        or canonical_json(protocol)
        != canonical_json(
            {
                "collection_exit_code": 0,
                "execution_exit_code": 0,
                "selection_args_allowed": False,
                "test_target": "tests",
            }
        )
        or commands["working_directory"] != str(REPOSITORY_ROOT.resolve())
        or commands["collection_argv"] != expected_collection
        or commands["environment"] != expected_environment
        or interpreter["executable"] != expected_executable
        or interpreter["executable_sha256"] != _sha256_file(executable)
        or interpreter["implementation"] != platform.python_implementation()
        or interpreter["python_version"] != platform.python_version()
        or interpreter["pytest_version"] != importlib.metadata.version("pytest")
        or layouts != current_layouts
    ):
        raise ReleaseGateError(
            "pytest attestation command, interpreter, or layout is invalid"
        )

    nodeids = collection["nodeids"]
    count = collection["count"]
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
        or not isinstance(nodeids, list)
        or len(nodeids) != count
        or len(set(nodeids)) != count
        or any(
            not isinstance(nodeid, str)
            or not nodeid.startswith("tests/")
            or "::" not in nodeid
            or "\x00" in nodeid
            for nodeid in nodeids
        )
    ):
        raise ReleaseGateError("pytest full-suite collection is invalid")
    expected_nodeids_sha256 = hashlib.sha256(
        canonical_json(nodeids).encode("utf-8")
    ).hexdigest()
    if collection["nodeids_sha256"] != expected_nodeids_sha256:
        raise ReleaseGateError("pytest collection nodeid hash is invalid")
    junit_counts = [
        junit[field]
        for field in (
            "suite_count",
            "testcase_count",
            "failures",
            "errors",
            "skipped",
        )
    ]
    _require_digest(junit["sha256"], "pytest attestation JUnit")
    if (
        any(
            not isinstance(item, int)
            or isinstance(item, bool)
            or item < 0
            for item in junit_counts
        )
        or junit["suite_count"] <= 0
        or junit["testcase_count"] != count
        or junit["failures"] != 0
        or junit["errors"] != 0
        or junit["skipped"] > count
    ):
        raise ReleaseGateError(
            "pytest collection and JUnit attestation do not reconcile"
        )
    return {
        "kind": obj["kind"],
        "collection_count": count,
        "nodeids_sha256": expected_nodeids_sha256,
        "junit_output_name": output_name,
        "junit_sha256": junit["sha256"],
        "junit_parent": str(temporary_parent),
        "suite_count": junit["suite_count"],
        "skipped": junit["skipped"],
        "source_layout_sha256": layouts["source_layout_sha256"],
        "test_layout_sha256": layouts["test_layout_sha256"],
    }


def _validate_junit_artifact(
    value: Any,
    path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    attestation = context["pytest_attestation"]["validation"]
    if not isinstance(value, bytes):
        raise ReleaseGateError("pytest JUnit bytes are unavailable")
    summary = _validate_junit_bytes(
        value,
        expected_count=attestation["collection_count"],
    )
    if (
        path.name != attestation["junit_output_name"]
        or hashlib.sha256(value).hexdigest() != attestation["junit_sha256"]
        or str(path.parent.resolve(strict=True)) != attestation["junit_parent"]
        or summary["suite_count"] != attestation["suite_count"]
        or summary["skipped"] != attestation["skipped"]
    ):
        raise ReleaseGateError("pytest JUnit is not bound to its attestation")
    return {"kind": "pytest_junit", **summary}


def _validate_junit(
    path: str | Path,
    *,
    expected_count: int | None = None,
) -> dict[str, int]:
    source = _safe_file(path, "pytest JUnit")
    encoded, _sha256 = _read_artifact_bytes(source, "pytest JUnit")
    return _validate_junit_bytes(encoded, expected_count=expected_count)


def _validate_junit_bytes(
    encoded: bytes,
    *,
    expected_count: int | None = None,
) -> dict[str, int]:
    if b"<!DOCTYPE" in encoded.upper() or b"<!ENTITY" in encoded.upper():
        raise ReleaseGateError("pytest JUnit contains an unsafe declaration")
    root = ET.fromstring(encoded)
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root)
        if not suites or any(child.tag != "testsuite" for child in suites):
            raise ReleaseGateError("pytest JUnit suite structure is invalid")
    else:
        raise ReleaseGateError("pytest JUnit root is invalid")

    tests = failures = errors = skipped = 0
    identities: set[tuple[str, str]] = set()
    for suite in suites:
        if suite.findall("testsuite"):
            raise ReleaseGateError("pytest JUnit nested suites are unsupported")
        cases = list(suite.findall("testcase"))
        suite_failures = suite_errors = suite_skipped = 0
        for case in cases:
            identity = (case.get("classname") or "", case.get("name") or "")
            if not all(identity) or identity in identities:
                raise ReleaseGateError("pytest JUnit testcase identities are invalid")
            identities.add(identity)
            outcomes = [
                child.tag
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ]
            if len(outcomes) > 1:
                raise ReleaseGateError("pytest JUnit testcase outcomes are invalid")
            suite_failures += outcomes.count("failure")
            suite_errors += outcomes.count("error")
            suite_skipped += outcomes.count("skipped")
            _validate_xml_duration(case.get("time", "0"), "testcase")
        supplied = {
            name: _xml_nonnegative_int(suite.get(name), f"suite {name}")
            for name in ("tests", "failures", "errors", "skipped")
        }
        calculated = {
            "tests": len(cases),
            "failures": suite_failures,
            "errors": suite_errors,
            "skipped": suite_skipped,
        }
        if supplied != calculated:
            raise ReleaseGateError(
                "pytest JUnit suite summary does not match testcases"
            )
        _validate_xml_duration(suite.get("time", "0"), "suite")
        tests += len(cases)
        failures += suite_failures
        errors += suite_errors
        skipped += suite_skipped

    calculated_total = {
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }
    if root.tag == "testsuites":
        supplied_values = {
            name: root.get(name) for name in calculated_total
        }
        if any(value is not None for value in supplied_values.values()):
            if any(value is None for value in supplied_values.values()):
                raise ReleaseGateError("pytest JUnit root summary is incomplete")
            supplied_total = {
                name: _xml_nonnegative_int(value, f"root {name}")
                for name, value in supplied_values.items()
            }
            if supplied_total != calculated_total:
                raise ReleaseGateError(
                    "pytest JUnit root summary does not match testcases"
                )
        _validate_xml_duration(root.get("time", "0"), "root")
    if tests <= 0 or (expected_count is not None and tests != expected_count):
        raise ReleaseGateError(
            "pytest JUnit testcase count does not match full-suite collection"
        )
    if failures or errors:
        raise ReleaseGateError("pytest JUnit contains failures or errors")
    return {
        **calculated_total,
        "suite_count": len(suites),
    }


def _xml_nonnegative_int(value: str | None, label: str) -> int:
    try:
        parsed = int(value or "")
    except ValueError as exc:
        raise ReleaseGateError(f"pytest JUnit {label} is invalid") from exc
    if parsed < 0 or str(parsed) != value:
        raise ReleaseGateError(f"pytest JUnit {label} is invalid")
    return parsed


def _validate_xml_duration(value: str, label: str) -> None:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ReleaseGateError(f"pytest JUnit {label} time is invalid") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ReleaseGateError(f"pytest JUnit {label} time is invalid")


def _validate_review_approval(
    value: Any,
    role: str,
    bundle_sha256: str,
) -> dict[str, Any]:
    obj = _object(value, f"{role} approval")
    required = {
        "schema_version",
        "kind",
        "reviewer_role",
        "reviewed_at_utc",
        "verdict",
        "approved",
        "release_eligible",
        "release_bundle_sha256",
        "open_p0_count",
        "open_p1_count",
    }
    _exact_keys(obj, required, f"{role} approval")
    reviewed_at = _parse_utc_timestamp(
        obj["reviewed_at_utc"],
        f"{role} approval timestamp",
    )
    if (
        obj["schema_version"] != 1
        or obj["kind"] != "v2_release_review_approval"
        or obj["reviewer_role"] != role
        or obj["verdict"] != "approve"
        or obj["approved"] is not True
        or obj["release_eligible"] is not False
        or obj["release_bundle_sha256"] != bundle_sha256
        or obj["open_p0_count"] != 0
        or obj["open_p1_count"] != 0
    ):
        raise ReleaseGateError(f"{role} approval does not match the release bundle")
    return {
        "kind": obj["kind"],
        "approved": True,
        "reviewed_at_utc": reviewed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _parse_approval_args(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        role, separator, path = value.partition("=")
        if not separator or role not in REQUIRED_REVIEW_ROLES or not path or role in result:
            raise ReleaseGateError("approval arguments must be unique ROLE=PATH values")
        result[role] = Path(path)
    return result


def _release_bundle_sha256(entries: Mapping[str, Mapping[str, Any]]) -> str:
    basis = {
        "schema_version": 2,
        "artifacts": {
            label: {
                "kind": entries[label]["kind"],
                "sha256": entries[label]["sha256"],
            }
            for label in PRIMARY_LABELS
        },
    }
    return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()


def _safe_file(
    value: str | Path,
    label: str,
    *,
    require_readonly: bool = False,
) -> Path:
    candidate = Path(value)
    if candidate.is_symlink():
        raise ReleaseGateError(f"{label} artifact is missing or unsafe")
    path = candidate.resolve(strict=True)
    if not path.is_file():
        raise ReleaseGateError(f"{label} artifact is missing or unsafe")
    if require_readonly and path.stat().st_mode & stat.S_IWRITE:
        raise ReleaseGateError(f"{label} artifact must be read-only")
    return path


def _retained_name(path: Path) -> str:
    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.name


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError(f"{label} artifact is unreadable") from exc
    return _object(value, label)


def _read_artifact_bytes(path: Path, label: str) -> tuple[bytes, str]:
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise ReleaseGateError(f"{label} artifact is unreadable") from exc
    return encoded, hashlib.sha256(encoded).hexdigest()


def _decode_json_artifact(encoded: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError(f"{label} artifact is unreadable") from exc
    return _object(value, label)


def _assert_artifact_unchanged(
    path: Path,
    label: str,
    expected_sha256: str,
) -> None:
    current = _safe_file(path, label, require_readonly=True)
    if _sha256_file(current) != expected_sha256:
        raise ReleaseGateError(f"{label} artifact changed during validation")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseGateError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ReleaseGateError(f"{label} fields are invalid")


def _require_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in _HEX for character in value)
    ):
        raise ReleaseGateError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _float32_bytes(values: list[Any]) -> bytes:
    try:
        numbers = [
            float(value)
            for value in values
            if not isinstance(value, bool)
        ]
        if len(numbers) != len(values) or any(
            not math.isfinite(value) for value in numbers
        ):
            raise ValueError("non-finite or boolean vector value")
        return struct.pack(f"<{len(numbers)}f", *numbers)
    except (OverflowError, TypeError, ValueError, struct.error) as exc:
        raise ReleaseGateError("query vector is not finite float32 data") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_immutable_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    try:
        target = run_release_pytest._new_output_path(
            path,
            REPOSITORY_ROOT,
            "release gate",
        )
    except run_release_pytest.ReleasePytestError as exc:
        raise ReleaseGateError(
            "release gate output path is unsafe or overlaps a source/test layout"
        ) from exc
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    temporary = target.parent / f".release-gate-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        temporary.unlink()
        os.chmod(target, stat.S_IREAD)
    finally:
        temporary.unlink(missing_ok=True)
    return target


_PRIMARY_VALIDATORS: dict[str, Validator] = {
    "conversion": _validate_conversion,
    "validation": _validate_conversion_validation,
    "reader_parity": _validate_reader_parity,
    "successor_race": _validate_successor_race,
    "launcher_matrix": _validate_launcher_matrix,
    "compatibility": _validate_compatibility,
    "compatibility_approval": _validate_compatibility_approval,
    "epoch_zero_performance": _validate_epoch_zero_performance,
    "successor_performance": _validate_successor_performance,
    "query_spec": _validate_query,
    "transition": _validate_transition,
    "installed_validation": _validate_installed_validation,
    "pytest_attestation": _validate_pytest_attestation,
    "pytest_junit": _validate_junit_artifact,
}


if __name__ == "__main__":
    raise SystemExit(main())
