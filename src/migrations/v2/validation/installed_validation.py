"""Exact schema validation for one-shot installed V2 release evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.migrations.v2.validation.performance import REQUIRED_WORKLOADS
from src.retrieval.identity import canonical_json


INSTALL_LABELS = ("source-default", "packaged-default")
REQUIRED_DISTRIBUTIONS = (
    "faiss-cpu",
    "numpy",
    "langchain-community",
    "langchain-core",
    "langchain-text-splitters",
    "PyMuPDF",
)


class InstalledValidationError(RuntimeError):
    """Raised when one-shot installed evidence does not satisfy its contract."""


def validate_installed_validation(
    value: Mapping[str, Any],
    *,
    query: Mapping[str, Any],
    query_spec_sha256: str,
    transition: Mapping[str, Any],
    transition_sha256: str,
    launcher_layout_sha256: str,
) -> dict[str, Any]:
    """Validate the installed artifact and every cross-artifact binding."""

    _exact_keys(
        value,
        {
            "schema_version",
            "kind",
            "passed",
            "fixture_only",
            "release_eligible",
            "release_gate_pending",
            "started_at",
            "completed_at",
            "environment",
            "timeout_seconds",
            "search_samples",
            "launcher_layout_sha256",
            "installed_environments",
            "installed_environments_sha256",
            "retained_evidence",
            "transition_run_id",
            "protected_tree_sha256",
            "source_tree_sha256",
            "baseline",
            "final",
            "validation",
        },
        "installed validation",
    )
    timeout_seconds = value.get("timeout_seconds")
    search_samples = value.get("search_samples")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != "v2_installed_validation"
        or value.get("passed") is not True
        or value.get("fixture_only") is not False
        or value.get("release_eligible") is not False
        or value.get("release_gate_pending")
        != "aggregate_release_gate_manifest"
        or not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 1 <= timeout_seconds <= 300
        or not isinstance(search_samples, int)
        or isinstance(search_samples, bool)
        or search_samples < 5
    ):
        raise InstalledValidationError("installed validation identity is invalid")

    started_at = _parse_utc_timestamp(value.get("started_at"), "start")
    completed_at = _parse_utc_timestamp(value.get("completed_at"), "completion")
    if completed_at < started_at:
        raise InstalledValidationError("installed validation timestamps are invalid")
    _validate_environment(value.get("environment"))

    expected_launcher = _require_digest(
        launcher_layout_sha256,
        "launcher-matrix layout",
    )
    if value.get("launcher_layout_sha256") != expected_launcher:
        raise InstalledValidationError(
            "installed validation is not bound to the launcher matrix"
        )
    installed_environment_sha256 = _validate_installed_environments(
        value.get("installed_environments"),
        value.get("installed_environments_sha256"),
    )

    retained = _object(value.get("retained_evidence"), "retained evidence")
    _exact_keys(retained, {"query", "transition"}, "retained evidence")
    expected_query_sha256 = _require_digest(
        query_spec_sha256,
        "query specification",
    )
    expected_transition_sha256 = _require_digest(
        transition_sha256,
        "transition evidence",
    )
    if retained != {
        "query": expected_query_sha256,
        "transition": expected_transition_sha256,
    }:
        raise InstalledValidationError("retained evidence hashes are invalid")
    if (
        value.get("transition_run_id") != transition.get("run_id")
        or value.get("protected_tree_sha256")
        != transition.get("protected_tree_sha256_after")
        or value.get("source_tree_sha256")
        != transition.get("source_tree_sha256_after")
        or transition.get("query_spec_sha256") != expected_query_sha256
    ):
        raise InstalledValidationError(
            "installed validation is not bound to transition input integrity"
        )
    _require_digest(value.get("protected_tree_sha256"), "protected tree")
    _require_digest(value.get("source_tree_sha256"), "source tree")

    transition_identity = _object(
        transition.get("final_runtime_identity"),
        "transition final runtime",
    )
    baseline = _validate_baseline(value.get("baseline"), transition_identity)
    final = _validate_baseline(value.get("final"), transition_identity)
    if canonical_json(final) != canonical_json(baseline):
        raise InstalledValidationError("installed validation final state drifted")

    validation = _object(value.get("validation"), "validation result")
    _exact_keys(
        validation,
        {
            "recorded_at",
            "passed",
            "guard_outcomes",
            "search_probes",
            "runtime_identity_sha256",
            "catalog_sha256",
            "snapshot_sha256",
        },
        "validation result",
    )
    recorded_at = _parse_utc_timestamp(
        validation.get("recorded_at"),
        "validation result timestamp",
    )
    if (
        validation.get("passed") is not True
        or recorded_at < started_at
        or recorded_at > completed_at
        or validation.get("runtime_identity_sha256")
        != hashlib.sha256(
            canonical_json(baseline["runtime_identity"]).encode("utf-8")
        ).hexdigest()
        or validation.get("catalog_sha256") != baseline["catalog_sha256"]
        or validation.get("snapshot_sha256") != baseline["snapshot_sha256"]
    ):
        raise InstalledValidationError("validation result identity is invalid")
    _validate_guard_outcomes(
        validation.get("guard_outcomes"),
        baseline["runtime_identity"],
    )
    probes = validation.get("search_probes")
    if not isinstance(probes, list) or len(probes) != len(INSTALL_LABELS):
        raise InstalledValidationError("installed search probes are incomplete")
    observed_installs: set[str] = set()
    build_identities: set[tuple[str, str]] = set()
    for probe in probes:
        install, build_identity = _validate_installed_probe(
            probe,
            query=query,
            query_file_sha256=expected_query_sha256,
            baseline=baseline,
            expected_samples=search_samples,
        )
        if install in observed_installs:
            raise InstalledValidationError("installed search probe is duplicated")
        observed_installs.add(install)
        build_identities.add(build_identity)
    if observed_installs != set(INSTALL_LABELS) or len(build_identities) != 1:
        raise InstalledValidationError(
            "installed search probe identities are inconsistent"
        )
    return {
        "kind": value["kind"],
        "transition_run_id": value["transition_run_id"],
        "runtime_identity": baseline["runtime_identity"],
        "launcher_layout_sha256": expected_launcher,
        "installed_environments_sha256": installed_environment_sha256,
        "query_spec_sha256": expected_query_sha256,
        "transition_sha256": expected_transition_sha256,
        "protected_tree_sha256": value["protected_tree_sha256"],
        "source_tree_sha256": value["source_tree_sha256"],
        "search_samples": search_samples,
        "passed": True,
    }


def _validate_environment(value: Any) -> None:
    environment = _object(value, "installed validation environment")
    _exact_keys(
        environment,
        {"os", "os_release", "python_version", "non_admin"},
        "installed validation environment",
    )
    if (
        environment.get("os") != "windows"
        or not isinstance(environment.get("os_release"), str)
        or not environment.get("os_release")
        or not isinstance(environment.get("python_version"), str)
        or not environment.get("python_version")
        or environment.get("non_admin") is not True
    ):
        raise InstalledValidationError("installed validation environment is invalid")


def _validate_baseline(
    value: Any,
    transition_identity: Mapping[str, Any],
) -> dict[str, Any]:
    baseline = _object(value, "installed baseline")
    _exact_keys(
        baseline,
        {
            "runtime_identity",
            "catalog_sha256",
            "catalog_logical_sha256",
            "snapshot_sha256",
            "snapshots",
            "writer_lock",
            "staging_entries",
        },
        "installed baseline",
    )
    runtime = _object(baseline.get("runtime_identity"), "installed runtime")
    _exact_keys(
        runtime,
        {
            "mode",
            "active_snapshot_id",
            "predecessor_snapshot_id",
            "publication_generation",
            "write_epoch",
            "v1_fallback_open",
            "degraded",
            "write_enabled",
        },
        "installed runtime",
    )
    for field in ("active_snapshot_id", "predecessor_snapshot_id"):
        _require_digest(runtime.get(field), f"installed {field}")
    if (
        runtime.get("mode") != "native"
        or runtime["active_snapshot_id"] == runtime["predecessor_snapshot_id"]
        or not isinstance(runtime.get("publication_generation"), int)
        or isinstance(runtime.get("publication_generation"), bool)
        or runtime["publication_generation"] <= 0
        or not isinstance(runtime.get("write_epoch"), int)
        or isinstance(runtime.get("write_epoch"), bool)
        or runtime["write_epoch"] <= 0
        or runtime.get("v1_fallback_open") is not False
        or runtime.get("degraded") is not False
        or runtime.get("write_enabled") is not True
        or {key: runtime.get(key) for key in transition_identity}
        != dict(transition_identity)
    ):
        raise InstalledValidationError("installed baseline runtime is invalid")
    for field in ("catalog_sha256", "catalog_logical_sha256", "snapshot_sha256"):
        _require_digest(baseline.get(field), f"installed baseline {field}")
    snapshots = _object(baseline.get("snapshots"), "installed snapshots")
    _exact_keys(snapshots, {"active", "predecessor"}, "installed snapshots")
    descriptors: dict[str, Mapping[str, Any]] = {}
    for role in ("active", "predecessor"):
        descriptor = _object(snapshots.get(role), f"installed {role} snapshot")
        _exact_keys(
            descriptor,
            {
                "snapshot_id",
                "relative_path",
                "sha256",
                "size_bytes",
                "dimension",
                "metric",
                "ntotal",
            },
            f"installed {role} snapshot",
        )
        _require_digest(descriptor.get("snapshot_id"), f"{role} snapshot ID")
        _require_digest(descriptor.get("sha256"), f"{role} snapshot SHA")
        relative = descriptor.get("relative_path")
        numeric = (
            descriptor.get("size_bytes"),
            descriptor.get("dimension"),
            descriptor.get("ntotal"),
        )
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith(("/", "\\"))
            or ".." in Path(relative).parts
            or any(
                not isinstance(item, int)
                or isinstance(item, bool)
                or item <= 0
                for item in numeric
            )
            or descriptor.get("metric") not in {"l2", "inner_product"}
            or descriptor.get("snapshot_id") != runtime[f"{role}_snapshot_id"]
        ):
            raise InstalledValidationError(
                f"installed {role} snapshot descriptor is invalid"
            )
        descriptors[role] = descriptor
    if (
        baseline.get("snapshot_sha256") != descriptors["active"]["sha256"]
        or descriptors["active"]["dimension"]
        != descriptors["predecessor"]["dimension"]
        or descriptors["active"]["metric"]
        != descriptors["predecessor"]["metric"]
        or baseline.get("writer_lock") is not False
        or baseline.get("staging_entries") != 0
    ):
        raise InstalledValidationError("installed baseline snapshot state is invalid")
    return dict(baseline)


def _validate_installed_environments(value: Any, supplied_sha256: Any) -> str:
    environments = _object(value, "installed environments")
    _exact_keys(environments, set(INSTALL_LABELS), "installed environments")
    semantic_hashes: set[str] = set()
    executable_hashes: set[str] = set()
    for label, environment_value in environments.items():
        environment = _object(environment_value, f"{label} environment")
        _exact_keys(
            environment,
            {
                "python_version",
                "implementation",
                "packages",
                "python_executable_sha256",
                "semantic_sha256",
            },
            f"{label} environment",
        )
        packages = _object(environment.get("packages"), f"{label} packages")
        _exact_keys(packages, set(REQUIRED_DISTRIBUTIONS), f"{label} packages")
        semantic = {
            "python_version": environment.get("python_version"),
            "implementation": environment.get("implementation"),
            "packages": dict(sorted(packages.items())),
        }
        calculated = hashlib.sha256(
            canonical_json(semantic).encode("utf-8")
        ).hexdigest()
        if (
            not isinstance(environment.get("python_version"), str)
            or not environment.get("python_version")
            or environment.get("implementation") != "CPython"
            or any(
                not isinstance(version, str) or not version
                for version in packages.values()
            )
            or environment.get("semantic_sha256") != calculated
        ):
            raise InstalledValidationError(
                "installed environment fingerprint is invalid"
            )
        semantic_hashes.add(calculated)
        executable_hashes.add(
            _require_digest(
                environment.get("python_executable_sha256"),
                f"{label} Python executable",
            )
        )
    calculated_sha256 = hashlib.sha256(
        canonical_json(environments).encode("utf-8")
    ).hexdigest()
    if (
        len(semantic_hashes) != 1
        or len(executable_hashes) != 1
        or supplied_sha256 != calculated_sha256
    ):
        raise InstalledValidationError("installed environments differ")
    return calculated_sha256


def _validate_guard_outcomes(
    value: Any,
    runtime_identity: Mapping[str, Any],
) -> None:
    if not isinstance(value, list) or len(value) != 4:
        raise InstalledValidationError("guard restart evidence is incomplete")
    combinations: set[tuple[str, bool]] = set()
    for outcome_value in value:
        outcome = _object(outcome_value, "guard outcome")
        _exact_keys(
            outcome,
            {
                "install",
                "write_guard",
                "duration_ns",
                "output_sha256",
                "runtime_identity",
            },
            "guard outcome",
        )
        combination = (outcome.get("install"), outcome.get("write_guard"))
        if (
            outcome.get("install") not in set(INSTALL_LABELS)
            or not isinstance(outcome.get("write_guard"), bool)
            or combination in combinations
            or not isinstance(outcome.get("duration_ns"), int)
            or isinstance(outcome.get("duration_ns"), bool)
            or outcome["duration_ns"] <= 0
            or canonical_json(outcome.get("runtime_identity"))
            != canonical_json(runtime_identity)
        ):
            raise InstalledValidationError("guard restart identity is invalid")
        _require_digest(outcome.get("output_sha256"), "guard output")
        combinations.add(combination)
    expected = {
        (label, write_guard)
        for label in INSTALL_LABELS
        for write_guard in (False, True)
    }
    if combinations != expected:
        raise InstalledValidationError("guard restart matrix is incomplete")


def _validate_installed_probe(
    value: Any,
    *,
    query: Mapping[str, Any],
    query_file_sha256: str,
    baseline: Mapping[str, Any],
    expected_samples: int,
) -> tuple[str, tuple[str, str]]:
    probe = _object(value, "installed search probe")
    _exact_keys(
        probe,
        {
            "schema_version",
            "kind",
            "passed",
            "query_id",
            "query_text_sha256",
            "query_vector_sha256",
            "query_spec_sha256",
            "query_generation",
            "runtime_identity",
            "workloads",
            "gate_d_search",
            "install",
            "duration_ns",
            "output_sha256",
        },
        "installed search probe",
    )
    if (
        probe.get("schema_version") != 1
        or probe.get("kind") != "v2_installed_validation_probe"
        or probe.get("passed") is not True
        or probe.get("install") not in set(INSTALL_LABELS)
        or probe.get("query_id") != query.get("query_id")
        or probe.get("query_text_sha256") != query.get("query_text_sha256")
        or probe.get("query_vector_sha256") != query.get("vector_sha256")
        or probe.get("query_spec_sha256") != query_file_sha256
        or not isinstance(probe.get("duration_ns"), int)
        or isinstance(probe.get("duration_ns"), bool)
        or probe["duration_ns"] <= 0
    ):
        raise InstalledValidationError("search probe query binding is invalid")
    _require_digest(probe.get("output_sha256"), "search probe output")
    generation = _object(probe.get("query_generation"), "query generation")
    _exact_keys(
        generation,
        {"provider", "model", "input_type", "provider_calls", "attestation_sha256"},
        "query generation",
    )
    if (
        generation.get("provider") != "openrouter"
        or generation.get("model") != query.get("model")
        or generation.get("input_type") != "search_query"
        or generation.get("provider_calls") != 1
        or isinstance(generation.get("provider_calls"), bool)
        or generation.get("attestation_sha256") != query.get("attestation_sha256")
    ):
        raise InstalledValidationError("query generation attestation is invalid")
    runtime = _object(probe.get("runtime_identity"), "probe runtime")
    _exact_keys(
        runtime,
        {
            "active_snapshot_id",
            "publication_generation",
            "active_build_id",
            "profile_id",
            "snapshot_sha256",
            "ntotal",
        },
        "probe runtime",
    )
    active = baseline["snapshots"]["active"]
    for field in (
        "active_snapshot_id",
        "active_build_id",
        "profile_id",
        "snapshot_sha256",
    ):
        _require_digest(runtime.get(field), f"probe {field}")
    if (
        runtime.get("active_snapshot_id")
        != baseline["runtime_identity"]["active_snapshot_id"]
        or runtime.get("publication_generation")
        != baseline["runtime_identity"]["publication_generation"]
        or runtime.get("snapshot_sha256") != active["sha256"]
        or runtime.get("ntotal") != active["ntotal"]
    ):
        raise InstalledValidationError("probe runtime identity is invalid")
    workloads = _object(probe.get("workloads"), "probe workloads")
    _exact_keys(workloads, set(REQUIRED_WORKLOADS), "probe workloads")
    for name in REQUIRED_WORKLOADS:
        _validate_workload(
            name,
            workloads[name],
            expected_samples=expected_samples,
            expected_ntotal=runtime["ntotal"],
            expected_report_uid=query["expected_report_uid"],
            expected_citation_sha256=query["expected_citation_sha256"],
        )
    gate_d = _object(probe.get("gate_d_search"), "Gate D result")
    _exact_keys(
        gate_d,
        {
            "expected_report_uid",
            "top_report_uid",
            "top_rank",
            "citation_complete",
            "citation_sha256",
        },
        "Gate D result",
    )
    if (
        gate_d.get("expected_report_uid") != query.get("expected_report_uid")
        or gate_d.get("top_report_uid") != query.get("expected_report_uid")
        or gate_d.get("top_rank") != 1
        or isinstance(gate_d.get("top_rank"), bool)
        or gate_d.get("citation_complete") is not True
        or gate_d.get("citation_sha256")
        != query.get("expected_citation_sha256")
    ):
        raise InstalledValidationError("Gate D result is invalid")
    return probe["install"], (runtime["active_build_id"], runtime["profile_id"])


def _validate_workload(
    name: str,
    value: Any,
    *,
    expected_samples: int,
    expected_ntotal: int,
    expected_report_uid: str,
    expected_citation_sha256: str,
) -> None:
    workload = _object(value, f"{name} workload")
    array_fields = (
        "strategies",
        "eligible_counts",
        "faiss_calls",
        "faiss_candidates",
        "hydration_batches",
        "hydration_rows",
        "top_report_uids",
        "top_chunk_uids",
        "citation_complete",
        "citation_sha256",
        "timings_ns",
    )
    _exact_keys(workload, {"samples", *array_fields}, f"{name} workload")
    if (
        not isinstance(expected_ntotal, int)
        or isinstance(expected_ntotal, bool)
        or expected_ntotal <= 0
        or workload.get("samples") != expected_samples
        or any(
            not isinstance(workload.get(field), list)
            or len(workload[field]) != expected_samples
            for field in array_fields
        )
    ):
        raise InstalledValidationError(f"{name} workload sample count is invalid")
    strategies = set(workload["strategies"])
    if any(
        strategy not in {"direct", "selector", "adaptive", "empty"}
        for strategy in strategies
    ):
        raise InstalledValidationError(f"{name} workload strategy is invalid")
    for field in (
        "eligible_counts",
        "faiss_calls",
        "faiss_candidates",
        "hydration_batches",
        "hydration_rows",
    ):
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in workload[field]
        ):
            raise InstalledValidationError(f"{name} workload counters are invalid")
    for item in workload["top_report_uids"] + workload["top_chunk_uids"]:
        if item is not None:
            _require_digest(item, f"{name} result ID")
    if any(not isinstance(item, bool) for item in workload["citation_complete"]):
        raise InstalledValidationError(f"{name} citation flags are invalid")
    for item in workload["citation_sha256"]:
        if item is not None:
            _require_digest(item, f"{name} citation")
    for timings_value in workload["timings_ns"]:
        timings = _object(timings_value, f"{name} timings")
        _exact_keys(
            timings,
            {"scope_compile", "eligibility", "faiss", "hydration", "lease", "total"},
            f"{name} timings",
        )
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in timings.values()
        ):
            raise InstalledValidationError(f"{name} timings are invalid")
    if name == "empty":
        if (
            any(strategy != "empty" for strategy in workload["strategies"])
            or any(
                workload[field] != [0] * expected_samples
                for field in (
                    "eligible_counts",
                    "faiss_calls",
                    "faiss_candidates",
                    "hydration_batches",
                    "hydration_rows",
                )
            )
            or any(
                item is not None
                for item in (
                    workload["top_report_uids"]
                    + workload["top_chunk_uids"]
                    + workload["citation_sha256"]
                )
            )
            or any(workload["citation_complete"])
        ):
            raise InstalledValidationError("empty workload executed retrieval")
    else:
        allowed_strategies = {
            "unfiltered": {"direct"},
            "narrow": {"selector"},
            "broad": {"direct", "adaptive"},
            "near_universe": {"direct", "adaptive"},
            "prior_scope": {"selector", "adaptive"},
        }[name]
        full_universe_strategy_invalid = name in {
            "unfiltered",
            "broad",
            "near_universe",
        } and any(
            strategy
            != ("direct" if eligible_count == expected_ntotal else "adaptive")
            for strategy, eligible_count in zip(
                workload["strategies"],
                workload["eligible_counts"],
            )
        )
        if (
            not strategies
            or not strategies.issubset(allowed_strategies)
            or full_universe_strategy_invalid
            or any(item <= 0 for item in workload["eligible_counts"])
            or any(item > expected_ntotal for item in workload["eligible_counts"])
            or any(item <= 0 for item in workload["faiss_calls"])
            or any(item <= 0 for item in workload["faiss_candidates"])
            or any(item is None for item in workload["top_report_uids"])
            or any(item is None for item in workload["top_chunk_uids"])
            or any(item is not True for item in workload["citation_complete"])
            or any(item is None for item in workload["citation_sha256"])
            or any(item["total"] <= 0 for item in workload["timings_ns"])
        ):
            raise InstalledValidationError(f"{name} workload retrieval is incomplete")
    if name == "narrow" and (
        any(item != expected_report_uid for item in workload["top_report_uids"])
        or any(item is not True for item in workload["citation_complete"])
        or any(
            item != expected_citation_sha256
            for item in workload["citation_sha256"]
        )
    ):
        raise InstalledValidationError("narrow Gate D workload is invalid")


def _parse_utc_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise InstalledValidationError(f"{label} is not a UTC timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise InstalledValidationError(f"{label} is not a UTC timestamp") from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InstalledValidationError(f"{label} must be a JSON object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise InstalledValidationError(f"{label} fields are invalid")


def _require_digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise InstalledValidationError(f"{label} is not a lowercase SHA-256 digest")
    return value


__all__ = [
    "INSTALL_LABELS",
    "REQUIRED_DISTRIBUTIONS",
    "InstalledValidationError",
    "validate_installed_validation",
]
