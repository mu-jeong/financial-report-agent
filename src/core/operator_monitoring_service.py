"""Application service for the single-admin reproduction and comparison cycle."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import statistics
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.core import fixed_snapshot, release_assets
from src.core.operator_monitoring import (
    MonitoringContractError,
    MonitoringRegistry,
)


class MonitoringServiceError(RuntimeError):
    """Raised when an official operation cannot be completed safely."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _control_digest(value: Mapping[str, Any]) -> str:
    """Hash local control state without copying its content to Supabase."""

    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _control_availability(value: Any) -> str:
    normalized = str(getattr(value, "value", value)).upper()
    return {
        "AVAILABLE": "AVAILABLE",
        "LOCAL_MISSING": "MISSING",
        "MISSING": "MISSING",
        "CORRUPT": "CORRUPT",
        "INCOMPATIBLE": "INCOMPATIBLE",
    }.get(normalized, "UNKNOWN")


def reproduction_seed_from_raw_report(
    raw_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep only the consented, bounded fields needed to prepare a Case."""

    observed = raw_report.get("observed")
    observed_body = observed if isinstance(observed, Mapping) else {}
    diagnostics = raw_report.get("case_diagnostics")
    diagnostics_body = diagnostics if isinstance(diagnostics, Mapping) else {}
    retrieval: list[dict[str, Any]] = []
    for raw in diagnostics_body.get("retrieval_observations") or []:
        if not isinstance(raw, Mapping):
            continue
        source_uid = str(raw.get("source_uid") or "")
        source_sha256 = str(raw.get("source_sha256") or "")
        if not _SHA256_RE.fullmatch(source_uid) or not _SHA256_RE.fullmatch(
            source_sha256
        ):
            continue
        item: dict[str, Any] = {
            "role": str(raw.get("role") or "OBSERVED_RESULT"),
            "source_uid": source_uid,
            "source_sha256": source_sha256,
            "rank": int(raw.get("rank") or len(retrieval) + 1),
        }
        chunk_uid = str(raw.get("chunk_uid") or "")
        chunk_sha256 = str(raw.get("chunk_sha256") or "")
        if _SHA256_RE.fullmatch(chunk_uid) and _SHA256_RE.fullmatch(
            chunk_sha256
        ):
            item.update(
                {"chunk_uid": chunk_uid, "chunk_sha256": chunk_sha256}
            )
        retrieval.append(item)
    route_observations = [
        {
            "selected_route": str(item.get("selected_route") or ""),
            "filters": dict(item.get("filters") or {}),
        }
        for item in diagnostics_body.get("route_observations") or []
        if isinstance(item, Mapping) and isinstance(item.get("filters") or {}, Mapping)
    ]
    return {
        "question": str(observed_body.get("selected_question") or ""),
        "reported_symptom": str(raw_report.get("comment") or ""),
        "observed_answer": str(observed_body.get("selected_answer") or ""),
        "case_diagnostics": {
            "schema_version": 1,
            "truncated": bool(diagnostics_body.get("truncated")),
            "route_observations": route_observations,
            "retrieval_observations": retrieval,
        },
    }


def snapshot_observed_report_uids(
    reproduction_seed: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return valid report identities recorded by production retrieval."""

    diagnostics = reproduction_seed.get("case_diagnostics")
    diagnostic_body = diagnostics if isinstance(diagnostics, Mapping) else {}
    return tuple(
        dict.fromkeys(
            str(item["source_uid"])
            for item in diagnostic_body.get("retrieval_observations") or []
            if isinstance(item, Mapping)
            and _SHA256_RE.fullmatch(str(item.get("source_uid") or ""))
        )
    )


def evaluate_typed_checks(
    fixture_body: Mapping[str, Any],
    run_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate the small v8 check allowlist without applying release thresholds."""

    answer = str(run_artifact.get("raw_answer") or "")
    evidence = run_artifact.get("evidence_refs")
    evidence_rows = evidence if isinstance(evidence, list) else []
    evidence_text = json.dumps(evidence_rows, ensure_ascii=False, sort_keys=True)
    route_summary = run_artifact.get("route_summary")
    route = (
        route_summary.get("route")
        if isinstance(route_summary, Mapping)
        else None
    )
    results: list[dict[str, Any]] = []
    for check in fixture_body.get("typed_checks") or []:
        if not isinstance(check, Mapping):
            continue
        check_type = str(check.get("type") or "")
        expected = str(check.get("value") or check.get("expected") or "")
        if check_type == "ANSWER_CONTAINS":
            passed: bool | None = expected in answer
        elif check_type == "ANSWER_NOT_CONTAINS":
            passed = expected not in answer
        elif check_type == "EVIDENCE_CONTAINS":
            passed = expected in evidence_text
        elif check_type == "CITATION_PRESENT":
            passed = any(
                isinstance(row, Mapping) and row.get("role") == "CITED"
                for row in evidence_rows
            )
        elif check_type == "ROUTE_EQUALS":
            passed = route == expected
        elif check_type == "MANUAL":
            passed = None
        else:
            raise MonitoringContractError(f"unsupported typed check: {check_type}")
        results.append(
            {
                "type": check_type,
                "expected": expected or None,
                "passed": passed,
            }
        )
    automatic = [row for row in results if row["passed"] is not None]
    passed = bool(automatic) and all(bool(row["passed"]) for row in automatic)
    return {
        "passed": passed,
        "reproduced": bool(automatic) and not passed,
        "checks": results,
        "manual_checks": list(fixture_body.get("manual_checks") or []),
        "manual_review_required": bool(fixture_body.get("manual_checks")),
    }


class ReleaseScopedMonitoringService:
    """Coordinate immutable registry records with managed local bytes."""

    def __init__(
        self,
        registry: MonitoringRegistry,
        *,
        managed_root: str | Path,
        project_root: str | Path | None = None,
        snapshot_availability: Callable[[str | Path, str], Any] | None = None,
    ) -> None:
        self.registry = registry
        self.managed_root = Path(managed_root).expanduser().resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root is not None
            else None
        )
        self._snapshot_availability = (
            snapshot_availability or fixed_snapshot.derive_fixed_snapshot_availability
        )

    @property
    def fixed_snapshot_root(self) -> Path:
        return self.managed_root / "fixed-snapshots"

    def import_remote_issue(self, issue: Mapping[str, Any]) -> dict[str, Any]:
        remote_id = str(issue.get("issue_id") or "").strip()
        if not remote_id:
            raise MonitoringServiceError("remote Issue has no issue_id")
        app_version = str(issue.get("app_version") or "unknown").removeprefix("v")
        reported_release = str(
            issue.get("reported_release_id") or f"release-v{app_version}"
        )
        summary_keys = {
            "question",
            "reported_problem",
            "app_version",
            "category",
            "kind",
            "report_target_type",
            "source",
            "route",
            "status",
            "latency_ms",
            "result_count",
            "citation_count",
            "case_diagnostics_status",
            "raw_available",
            "received_at",
        }
        summary = {key: issue.get(key) for key in summary_keys if key in issue}
        if not summary:
            summary = {"app_version": app_version}
        return self.registry.create_issue(
            source_receipt_id=f"supabase:{remote_id}",
            reported_release_id=reported_release,
            summary=summary,
        )

    def register_fixed_snapshot(
        self, snapshot: fixed_snapshot.FixedSnapshot
    ) -> dict[str, Any]:
        try:
            relative = snapshot.path.resolve().relative_to(self.managed_root)
        except ValueError as exc:
            raise MonitoringServiceError(
                "FixedSnapshot must be below the monitoring managed root"
            ) from exc
        manifest = dict(snapshot.manifest)
        return self.registry.register_fixed_snapshot(
            fixed_snapshot_revision_id=snapshot.revision_id,
            bundle_relpath=relative.as_posix(),
            bundle_digest=snapshot.revision_id,
            manifest=manifest,
            reader_contract={
                "contract": manifest.get("reader_contract"),
                "manifest_schema_version": manifest.get(
                    "manifest_schema_version"
                ),
                "dimension": (manifest.get("vector") or {}).get("dimension")
                if isinstance(manifest.get("vector"), Mapping)
                else None,
                "metric": (manifest.get("vector") or {}).get("metric")
                if isinstance(manifest.get("vector"), Mapping)
                else None,
            },
        )

    def propose_snapshot_scope(
        self,
        reproduction_seed: Mapping[str, Any],
        *,
        data_root: str | Path,
    ) -> fixed_snapshot.SnapshotScopeProposal:
        diagnostics = reproduction_seed.get("case_diagnostics")
        diagnostic_body = diagnostics if isinstance(diagnostics, Mapping) else {}
        filters: dict[str, Any] = {}
        for route in diagnostic_body.get("route_observations") or []:
            if isinstance(route, Mapping) and isinstance(route.get("filters"), Mapping):
                filters.update(dict(route["filters"]))
        catalog, _ = fixed_snapshot.resolve_active_snapshot_sources(data_root)
        return fixed_snapshot.propose_report_scope(
            catalog,
            observed_report_uids=snapshot_observed_report_uids(
                reproduction_seed
            ),
            filters=filters,
        )

    def list_snapshot_documents(
        self,
        *,
        data_root: str | Path,
    ) -> tuple[fixed_snapshot.ActiveReportDocument, ...]:
        """Return metadata-only active documents for human scope selection."""

        catalog, _ = fixed_snapshot.resolve_active_snapshot_sources(data_root)
        return fixed_snapshot.list_active_report_documents(catalog)

    def create_fixed_snapshot_for_case(
        self,
        *,
        data_root: str | Path,
        report_uids: Sequence[str],
    ) -> tuple[fixed_snapshot.FixedSnapshot, dict[str, Any]]:
        catalog, source_index = fixed_snapshot.resolve_active_snapshot_sources(
            data_root
        )
        snapshot = fixed_snapshot.create_fixed_snapshot(
            catalog,
            source_index,
            self.fixed_snapshot_root,
            report_uids=report_uids,
        )
        return snapshot, self.register_fixed_snapshot(snapshot)

    def build_reconstruction_lineage(
        self,
        reproduction_seed: Mapping[str, Any],
        *,
        fixed_snapshot_revision_id: str,
        operator_scope_confirmed: bool = False,
        operator_scope_reason: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.registry.get_fixed_snapshot(fixed_snapshot_revision_id)
        snapshot_path = self._assert_snapshot_available(snapshot)
        catalog_path = (
            snapshot_path / fixed_snapshot.CATALOG_FILENAME
        ).resolve(strict=True)
        with sqlite3.connect(
            f"{catalog_path.as_uri()}?mode=ro",
            uri=True,
        ) as connection:
            rows = connection.execute(
                "SELECT report_uid, source_sha256 FROM reports ORDER BY report_uid"
            ).fetchall()
        snapshot_sources = {str(uid): str(digest) for uid, digest in rows}
        diagnostics = reproduction_seed.get("case_diagnostics")
        diagnostic_body = diagnostics if isinstance(diagnostics, Mapping) else {}
        observed_sources = {
            str(item["source_uid"]): str(item["source_sha256"])
            for item in diagnostic_body.get("retrieval_observations") or []
            if isinstance(item, Mapping)
            and _SHA256_RE.fullmatch(str(item.get("source_uid") or ""))
            and _SHA256_RE.fullmatch(str(item.get("source_sha256") or ""))
        }
        if not observed_sources:
            reason = str(operator_scope_reason or "").strip()
            return {
                "schema_version": 1,
                "basis": "OPERATOR_DEFINED",
                "exact_count": 0,
                "observed_count": 0,
                "exceptions": [],
                "operator_scope_confirmed": bool(operator_scope_confirmed),
                "operator_scope_reason": reason,
                "evidence_qualifier": "PARTIAL",
            }

        exact: list[dict[str, str]] = []
        exceptions: list[dict[str, Any]] = []
        for source_uid, observed_digest in sorted(observed_sources.items()):
            snapshot_digest = snapshot_sources.get(source_uid)
            if snapshot_digest is None:
                exceptions.append(
                    {
                        "kind": "MISSING",
                        "source_uid": source_uid,
                        "observed_source_sha256": observed_digest,
                        "reason": "Snapshot 범위에 신고 관찰 자료가 없습니다.",
                        "confirmed": False,
                    }
                )
            elif snapshot_digest != observed_digest:
                exceptions.append(
                    {
                        "kind": "CONTENT_DIFFERENT",
                        "source_uid": source_uid,
                        "observed_source_sha256": observed_digest,
                        "snapshot_source_sha256": snapshot_digest,
                        "confirmed": False,
                    }
                )
            else:
                exact.append(
                    {
                        "source_uid": source_uid,
                        "source_sha256": observed_digest,
                    }
                )
        exact_digest = hashlib.sha256(
            json.dumps(
                exact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": 1,
            "basis": "REPORT_DIAGNOSTICS",
            "diagnostics_truncated": bool(diagnostic_body.get("truncated")),
            "observed_count": len(observed_sources),
            "exact_count": len(exact),
            "exact_mapping_digest": exact_digest,
            "exceptions": exceptions,
            "evidence_qualifier": "PARTIAL" if exceptions else "EXACT",
        }

    def register_release(
        self,
        descriptor: release_assets.ReleaseDescriptor,
        *,
        release_tag: str,
    ) -> dict[str, Any]:
        try:
            relative = descriptor.path.resolve().relative_to(self.managed_root)
        except ValueError as exc:
            raise MonitoringServiceError(
                "Release bundle must be below the monitoring managed root"
            ) from exc
        manifest = json.loads(
            (descriptor.path / "release-manifest.json").read_text(encoding="utf-8")
        )
        if not isinstance(manifest, dict):
            raise MonitoringServiceError("Release manifest must be an object")
        manifest.update(
            {
                "runner_contract_version": descriptor.runner_contract_version,
                "build_digest": descriptor.build_digest,
                "git_revision": descriptor.git_revision,
                "snapshot_reader_contract_version": (
                    descriptor.snapshot_reader_contract_version
                ),
            }
        )
        if descriptor.manifest_version == release_assets.RELEASE_SCHEMA_VERSION:
            manifest["runtime_profile_digest"] = descriptor.runtime_profile_digest
        else:
            manifest.pop("runtime_profile_digest", None)
        return self.registry.register_release_manifest(
            release_manifest_id=descriptor.release_manifest_id,
            release_tag=release_tag,
            app_version=descriptor.app_version,
            manifest_version=descriptor.manifest_version,
            runtime_bundle_digest=descriptor.runtime_bundle_digest,
            bundle_relpath=relative.as_posix(),
            manifest=manifest,
        )

    def _release_descriptor(self, release_manifest_id: str) -> release_assets.ReleaseDescriptor:
        record = self.registry.get_release_manifest(release_manifest_id)
        manifest = record["manifest"]
        return release_assets.ReleaseDescriptor(
            release_manifest_id=record["release_manifest_id"],
            app_version=record["app_version"],
            git_revision=str(manifest.get("git_revision") or ""),
            build_digest=str(manifest.get("build_digest") or ""),
            runtime_bundle_digest=record["runtime_bundle_digest"],
            runtime_profile_digest=str(
                manifest.get("runtime_profile_digest") or ""
            ),
            runner_contract_version=int(
                manifest.get("runner_contract_version") or 0
            ),
            snapshot_reader_contract_version=int(
                manifest.get("snapshot_reader_contract_version") or 0
            ),
            path=self.managed_root / record["bundle_relpath"],
            manifest_version=int(record["manifest_version"]),
        )

    def build_control_projection(
        self,
        issue_id: str,
        *,
        include_release_manifest_ids: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Build the bounded Supabase projection for one local Issue.

        The projection contains lifecycle state, identities, digests, categorical
        judgments, and derived availability only. Fixture/Case bodies, answers,
        evidence, filesystem paths, and runtime profiles remain local.
        """

        issue = self.registry.get_issue(issue_id)
        fixtures = self.registry.list_fixture_revisions(issue_id)
        cases = self.registry.list_case_revisions(issue_id)
        runs = self.registry.list_runs(issue_id=issue_id)
        comparisons = self.registry.list_comparisons(issue_id)

        snapshot_ids = {
            str(case["fixed_snapshot_revision_id"])
            for case in cases
        }
        releases_by_id = {
            str(record["release_manifest_id"]): record
            for record in self.registry.list_release_manifests()
        }
        release_ids = {str(run["release_manifest_id"]) for run in runs}
        for release_id in include_release_manifest_ids:
            normalized = str(release_id)
            if normalized not in releases_by_id:
                raise MonitoringServiceError(
                    f"Release is not registered locally: {normalized}"
                )
            release_ids.add(normalized)
        reported_release_id = str(issue.get("reported_release_id") or "")
        if reported_release_id in releases_by_id:
            release_ids.add(reported_release_id)
        else:
            reported_version = reported_release_id.removeprefix(
                "release-"
            ).removeprefix("v")
            for release_id, record in releases_by_id.items():
                if str(record.get("app_version") or "") == reported_version:
                    release_ids.add(release_id)

        records: list[dict[str, Any]] = []
        for fixture in fixtures:
            status = str(fixture["lifecycle_status"])
            digest = str(fixture.get("fixture_digest") or "")
            if not _SHA256_RE.fullmatch(digest):
                digest = _control_digest(
                    {
                        "record_kind": "FIXTURE",
                        "record_id": fixture["fixture_revision_id"],
                        "body": fixture["body"],
                    }
                )
            records.append(
                {
                    "record_kind": "FIXTURE",
                    "record_id": str(fixture["fixture_revision_id"]),
                    "lifecycle_status": status,
                    "content_digest": digest,
                    "availability": None,
                    "references": {},
                    "attributes": {},
                }
            )

        cases_by_contract: dict[str, Mapping[str, Any]] = {}
        for case in cases:
            status = str(case["lifecycle_status"])
            case_contract_id = str(case.get("case_contract_id") or "")
            if _SHA256_RE.fullmatch(case_contract_id):
                digest = case_contract_id
                cases_by_contract[case_contract_id] = case
            else:
                digest = _control_digest(
                    {
                        "record_kind": "CASE",
                        "record_id": case["case_revision_id"],
                        "fixture_revision_id": case["fixture_revision_id"],
                        "fixed_snapshot_revision_id": case[
                            "fixed_snapshot_revision_id"
                        ],
                        "fixed_clock": case.get("fixed_clock"),
                        "evaluator": case["evaluator"],
                        "reconstruction_lineage": case[
                            "reconstruction_lineage"
                        ],
                    }
                )
            references = {
                "fixture_revision_id": str(case["fixture_revision_id"]),
                "fixed_snapshot_revision_id": str(
                    case["fixed_snapshot_revision_id"]
                ),
            }
            attributes: dict[str, str] = {}
            if case_contract_id:
                references["case_contract_id"] = case_contract_id
            qualifier = str(case.get("evidence_qualifier") or "")
            if qualifier:
                attributes["evidence_qualifier"] = qualifier
            records.append(
                {
                    "record_kind": "CASE",
                    "record_id": str(case["case_revision_id"]),
                    "lifecycle_status": status,
                    "content_digest": digest,
                    "availability": None,
                    "references": references,
                    "attributes": attributes,
                }
            )

        for snapshot_id in sorted(snapshot_ids):
            snapshot = self.registry.get_fixed_snapshot(snapshot_id)
            path = self.managed_root / str(snapshot["bundle_relpath"])
            try:
                availability = _control_availability(
                    self._snapshot_availability(path.parent, snapshot_id)
                )
            except (OSError, ValueError, fixed_snapshot.FixedSnapshotError):
                availability = "UNKNOWN"
            records.append(
                {
                    "record_kind": "FIXED_SNAPSHOT",
                    "record_id": snapshot_id,
                    "lifecycle_status": "READY",
                    "content_digest": str(snapshot["bundle_digest"]),
                    "availability": availability,
                    "references": {},
                    "attributes": {},
                }
            )

        for release_id in sorted(release_ids):
            release = releases_by_id[release_id]
            try:
                availability = _control_availability(
                    release_assets.inspect_release(
                        self._release_descriptor(release_id)
                    )
                )
            except (OSError, ValueError, release_assets.ReleaseAssetError):
                availability = "UNKNOWN"
            records.append(
                {
                    "record_kind": "RELEASE",
                    "record_id": release_id,
                    "lifecycle_status": "REGISTERED",
                    "content_digest": (
                        release_id
                        if int(release["manifest_version"]) == 2
                        else str(release["runtime_bundle_digest"])
                    ),
                    "availability": availability,
                    "references": {},
                    "attributes": {},
                }
            )

        for run in runs:
            case_contract_id = str(run["case_contract_id"])
            case = cases_by_contract.get(case_contract_id)
            if case is None:
                case = self.registry.get_case_by_contract(case_contract_id)
            status = str(run["execution_status"])
            artifact_digest = str(run.get("artifact_digest") or "")
            digest = (
                artifact_digest
                if _SHA256_RE.fullmatch(artifact_digest)
                else _control_digest(
                    {
                        "record_kind": "RUN",
                        "run_id": run["run_id"],
                        "case_contract_id": case_contract_id,
                        "release_manifest_id": run["release_manifest_id"],
                        "side": run["side"],
                    }
                )
            )
            availability: str | None = None
            if run.get("artifact_relpath"):
                artifact_path = (
                    self.registry.artifact_root / str(run["artifact_relpath"])
                ).resolve()
                try:
                    artifact_path.relative_to(self.registry.artifact_root)
                except ValueError:
                    availability = "CORRUPT"
                else:
                    try:
                        if not artifact_path.is_file():
                            availability = "MISSING"
                        elif (
                            hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                            != artifact_digest
                        ):
                            availability = "CORRUPT"
                        else:
                            availability = "AVAILABLE"
                    except OSError:
                        availability = "UNKNOWN"
            attributes = {
                "side": str(run["side"]),
                "validity": str(run.get("validity") or "UNKNOWN"),
                "evidence_qualifier": str(
                    case.get("evidence_qualifier") or "UNKNOWN"
                ),
            }
            records.append(
                {
                    "record_kind": "RUN",
                    "record_id": str(run["run_id"]),
                    "lifecycle_status": status,
                    "content_digest": digest,
                    "availability": availability,
                    "references": {
                        "fixed_snapshot_revision_id": str(
                            case["fixed_snapshot_revision_id"]
                        ),
                        "release_manifest_id": str(
                            run["release_manifest_id"]
                        ),
                        "case_contract_id": case_contract_id,
                    },
                    "attributes": attributes,
                }
            )

        for comparison in comparisons:
            references: dict[str, str] = {
                "case_contract_id": str(comparison["case_contract_id"]),
            }
            predecessor = str(
                comparison.get("supersedes_comparison_id") or ""
            )
            if predecessor:
                references["supersedes_comparison_id"] = predecessor
            records.append(
                {
                    "record_kind": "COMPARISON",
                    "record_id": str(comparison["comparison_id"]),
                    "lifecycle_status": "CREATED",
                    "content_digest": str(comparison["record_digest"]),
                    "availability": None,
                    "references": references,
                    "attributes": {"verdict": str(comparison["verdict"])},
                }
            )

        order = {
            "FIXTURE": 0,
            "FIXED_SNAPSHOT": 1,
            "CASE": 2,
            "RELEASE": 3,
            "RUN": 4,
            "COMPARISON": 5,
        }
        return sorted(
            records,
            key=lambda record: (
                order[str(record["record_kind"])],
                str(record["record_id"]),
            ),
        )

    def _assert_snapshot_available(self, snapshot_record: Mapping[str, Any]) -> Path:
        path = self.managed_root / str(snapshot_record["bundle_relpath"])
        availability = self._snapshot_availability(
            path.parent,
            str(snapshot_record["fixed_snapshot_revision_id"]),
        )
        value = getattr(availability, "value", str(availability))
        if value != fixed_snapshot.FixedSnapshotAvailability.AVAILABLE.value:
            raise MonitoringServiceError(
                f"FixedSnapshot is unavailable: {value}"
            )
        return path

    @staticmethod
    def _registered_runtime_profile(
        descriptor: release_assets.ReleaseDescriptor,
    ) -> dict[str, Any]:
        try:
            value = json.loads(
                (descriptor.path / "runtime-profile.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise MonitoringServiceError(
                "registered runtime profile is unreadable"
            ) from exc
        if not isinstance(value, dict):
            raise MonitoringServiceError(
                "registered runtime profile must be an object"
            )
        return value

    def _assert_release_snapshot_compatible(
        self,
        descriptor: release_assets.ReleaseDescriptor,
        snapshot_record: Mapping[str, Any],
        runtime_profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        manifest = snapshot_record.get("manifest")
        manifest_body = manifest if isinstance(manifest, Mapping) else {}
        manifest_version = int(
            manifest_body.get("manifest_schema_version")
            or manifest_body.get("schema_version")
            or 0
        )
        if descriptor.snapshot_reader_contract_version != manifest_version:
            raise MonitoringServiceError(
                "Release and FixedSnapshot reader contracts are incompatible"
            )
        profile = dict(runtime_profile)
        reader = profile.get("snapshot_reader")
        if reader is not None and not isinstance(reader, Mapping):
            raise MonitoringServiceError(
                "runtime profile snapshot_reader must be an object"
            )
        if isinstance(reader, Mapping):
            expected_manifest = reader.get("manifest_schema_version")
            if expected_manifest is not None and int(expected_manifest) != manifest_version:
                raise MonitoringServiceError(
                    "runtime profile rejects the FixedSnapshot manifest"
                )
            vector = manifest_body.get("vector")
            vector_body = vector if isinstance(vector, Mapping) else {}
            for key in ("dimension", "metric"):
                expected = reader.get(key)
                if expected is not None and expected != vector_body.get(key):
                    raise MonitoringServiceError(
                        f"runtime profile snapshot {key} is incompatible"
                    )
            expected_contract = reader.get("reader_contract")
            if (
                expected_contract is not None
                and expected_contract != manifest_body.get("reader_contract")
            ):
                raise MonitoringServiceError(
                    "runtime profile snapshot reader_contract is incompatible"
                )
        return profile

    def mark_case_ready(self, case_revision_id: str) -> dict[str, Any]:
        case = self.registry.get_case_revision(case_revision_id)
        snapshot = self.registry.get_fixed_snapshot(
            case["fixed_snapshot_revision_id"]
        )
        available = True
        try:
            self._assert_snapshot_available(snapshot)
        except MonitoringServiceError:
            available = False
        return self.registry.mark_case_ready(
            case_revision_id,
            snapshot_available=available,
        )

    def recover_incomplete_runs(
        self, *, operator_confirmed_no_active_process: bool = False
    ) -> list[dict[str, Any]]:
        """Seal orphaned Runs only after the single operator checks ownership."""

        if operator_confirmed_no_active_process is not True:
            raise MonitoringServiceError(
                "incomplete Run recovery requires confirmation that no runner "
                "or other Monitoring process is active"
            )

        recovered: list[dict[str, Any]] = []
        for issue in self.registry.list_issues():
            for run in self.registry.list_runs(issue_id=str(issue["issue_id"])):
                status = str(run["execution_status"])
                if status not in {"QUEUED", "RUNNING"}:
                    continue
                if status == "QUEUED":
                    run = self.registry.start_run(str(run["run_id"]))
                recovered.append(
                    self.registry.finish_run(
                        str(run["run_id"]),
                        execution_status="INTERRUPTED",
                        validity="INVALID",
                        artifact={
                            "invalid_reason": (
                                "operator process ended before the Run reached "
                                "a terminal result"
                            ),
                            "recovery": "OPERATOR_CONFIRMED",
                        },
                    )
                )
        return recovered

    def execute_run(
        self,
        *,
        issue_id: str,
        case_contract_id: str,
        release_manifest_id: str,
        side: str,
        runtime_profile: Mapping[str, Any] | None = None,
        timeout_seconds: float = 300.0,
        extra_environment: Mapping[str, str] | None = None,
        lifecycle_callback: Callable[[Mapping[str, Any]], None] | None = None,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        total_steps = 7
        projection_sync_warnings: list[str] = []

        def notify_lifecycle(
            run: Mapping[str, Any], *, best_effort: bool = False
        ) -> None:
            if lifecycle_callback is None:
                return
            try:
                lifecycle_callback(run)
            except Exception as exc:
                if not best_effort:
                    raise
                status = str(run.get("execution_status") or "UNKNOWN")
                projection_sync_warnings.append(
                    f"{status}: {type(exc).__name__}: {str(exc)[:500]}"
                )

        def report_progress(
            stage: str,
            message: str,
            step: int,
            *,
            run: Mapping[str, Any] | None = None,
        ) -> None:
            if progress_callback is None:
                return
            event: dict[str, Any] = {
                "stage": stage,
                "message": message,
                "step": step,
                "total_steps": total_steps,
            }
            if run is not None:
                event["run_id"] = run.get("run_id")
                event["execution_status"] = run.get("execution_status")
                event["side"] = run.get("side")
            try:
                progress_callback(event)
            except Exception:
                # Progress is observational. A broken UI observer must never
                # leave an otherwise finishable Run stuck in RUNNING.
                return

        report_progress(
            "PREFLIGHT",
            "실행 조건과 미완료 Run을 확인하고 있습니다.",
            1,
        )
        try:
            incomplete = [
                run
                for run in self.registry.list_runs(issue_id=issue_id)
                if run["execution_status"] in {"QUEUED", "RUNNING"}
            ]
            if incomplete:
                raise MonitoringServiceError(
                    "an incomplete Run must be recovered before starting another Run"
                )
            case = self.registry.get_case_by_contract(case_contract_id)
            fixture = self.registry.get_fixture_revision(case["fixture_revision_id"])
            snapshot = self.registry.get_fixed_snapshot(
                case["fixed_snapshot_revision_id"]
            )
            snapshot_path = self._assert_snapshot_available(snapshot)
            descriptor = self._release_descriptor(release_manifest_id)
            release_status = release_assets.inspect_release(descriptor)
            if (
                release_status is release_assets.ReleaseAvailability.LOCAL_MISSING
                and descriptor.manifest_version
                == release_assets.GIT_RELEASE_SCHEMA_VERSION
                and self.project_root is not None
            ):
                descriptor = release_assets.rebuild_release_cache_from_git(
                    self.managed_root,
                    project_root=self.project_root,
                    descriptor=descriptor,
                )
                release_status = release_assets.inspect_release(descriptor)
            if release_status is not release_assets.ReleaseAvailability.AVAILABLE:
                raise MonitoringServiceError(
                    f"Release bundle is unavailable: {release_status.value}"
                )
            if runtime_profile is not None:
                selected_profile = dict(runtime_profile)
            elif descriptor.manifest_version == release_assets.RELEASE_SCHEMA_VERSION:
                selected_profile = self._registered_runtime_profile(descriptor)
            else:
                raise MonitoringServiceError(
                    "Git Release execution requires an explicit Run profile"
                )
            release_assets.validate_runtime_profile(selected_profile)
            selected_profile = self._assert_release_snapshot_compatible(
                descriptor,
                snapshot,
                selected_profile,
            )
        except BaseException as exc:
            report_progress(
                "FAILED",
                f"실행 준비에 실패했습니다: {str(exc)[:500]}",
                total_steps,
            )
            raise
        report_progress(
            "ASSETS_READY",
            "Release bundle과 FixedSnapshot 검증을 마쳤습니다.",
            2,
        )
        queued = self.registry.queue_run(
            issue_id=issue_id,
            case_contract_id=case_contract_id,
            release_manifest_id=release_manifest_id,
            side=side,
            runtime_profile=selected_profile,
        )
        report_progress(
            "QUEUED",
            "Run을 등록하고 실행 대기 상태로 저장했습니다.",
            3,
            run=queued,
        )
        notify_lifecycle(queued)
        running = self.registry.start_run(queued["run_id"])
        notify_lifecycle(running)
        report_progress(
            "EXECUTING",
            "등록된 Release를 고정된 재현 조건으로 실행하고 있습니다.",
            4,
            run=running,
        )

        terminal_status = "FAILED"
        validity = "INVALID"
        artifact: dict[str, Any]
        execution_error: BaseException | None = None
        try:
            execution = release_assets.execute_registered_release(
                self.managed_root,
                descriptor,
                snapshot_root=snapshot_path,
                run_id=queued["run_id"],
                input_payload={
                    "question": fixture["body"]["question"],
                    "fixed_clock": case.get("fixed_clock"),
                    "case_contract_id": case_contract_id,
                    "fixed_snapshot_revision_id": case[
                        "fixed_snapshot_revision_id"
                    ],
                },
                timeout_seconds=timeout_seconds,
                extra_environment=extra_environment,
                runtime_profile=selected_profile,
            )
            runner_artifact = json.loads(
                execution.artifact_path.read_text(encoding="utf-8")
            )
            if not isinstance(runner_artifact, dict):
                raise MonitoringServiceError("runner artifact must be an object")
            report_progress(
                "VALIDATING_RESULT",
                "실행 artifact와 검사 결과를 검증하고 있습니다.",
                5,
                run=running,
            )
            succeeded = (
                execution.returncode == 0
                and runner_artifact.get("runner_status") == "SUCCEEDED"
            )
            if succeeded:
                check_result = evaluate_typed_checks(
                    fixture["body"], runner_artifact
                )
                actual_profile = runner_artifact.get("runtime_profile")
                terminal_status = "SUCCEEDED"
                validity = "VALID"
                invalid_reason = None
                if actual_profile != queued["runtime_profile"]:
                    validity = "INVALID"
                    invalid_reason = (
                        "runner runtime profile differs from queued Run input"
                    )
                artifact = {
                    "raw_answer": runner_artifact.get("raw_answer", ""),
                    "evidence_refs": runner_artifact.get("evidence_refs", []),
                    "route_summary": runner_artifact.get("route_summary", {}),
                    "check_result": check_result,
                    "runtime_profile": (
                        actual_profile if isinstance(actual_profile, Mapping) else {}
                    ),
                    "latency_ms": runner_artifact.get("latency_ms", 0),
                    "runner_artifact_digest": execution.artifact_digest,
                    "cleanup_warning": execution.cleanup_warning,
                    "evidence_qualifier": case.get("evidence_qualifier"),
                }
                if invalid_reason:
                    artifact["invalid_reason"] = invalid_reason
            else:
                artifact = {
                    "runner_result": runner_artifact,
                    "runner_artifact_digest": execution.artifact_digest,
                    "cleanup_warning": execution.cleanup_warning,
                }
        except BaseException as exc:
            execution_error = exc
            artifact = {
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:1000],
            }

        report_progress(
            "SAVING_RESULT",
            "검증 결과와 실행 artifact를 불변 Run 기록으로 저장하고 있습니다.",
            6,
            run=running,
        )
        terminal = self.registry.finish_run(
            queued["run_id"],
            execution_status=terminal_status,
            validity=validity,
            artifact=artifact,
        )
        notify_lifecycle(terminal, best_effort=True)
        if projection_sync_warnings:
            terminal = {
                **terminal,
                "projection_sync_warnings": list(projection_sync_warnings),
            }
        report_progress(
            terminal_status,
            (
                "Run 실행과 결과 저장을 완료했습니다."
                if terminal_status == "SUCCEEDED"
                else "Run 실행이 실패했습니다. 저장된 오류 결과를 확인하세요."
            ),
            7,
            run=terminal,
        )
        if execution_error is not None:
            if isinstance(execution_error, Exception):
                raise MonitoringServiceError(
                    f"Run {terminal['run_id']} failed: "
                    f"{artifact.get('error_message') or type(execution_error).__name__}"
                ) from execution_error
            raise execution_error
        return terminal

    def comparison_view(
        self,
        *,
        baseline_run_ids: Sequence[str],
        candidate_run_ids: Sequence[str],
    ) -> dict[str, Any]:
        baseline = [self.registry.get_run(run_id) for run_id in baseline_run_ids]
        candidate = [self.registry.get_run(run_id) for run_id in candidate_run_ids]
        if not baseline or not candidate:
            raise MonitoringServiceError(
                "official view requires both Baseline and Candidate Runs"
            )
        selected = baseline + candidate
        if any(
            row["execution_status"] != "SUCCEEDED" or row["validity"] != "VALID"
            for row in selected
        ):
            raise MonitoringServiceError(
                "official view accepts only SUCCEEDED + VALID Runs"
            )
        if len({row["case_contract_id"] for row in selected}) != 1:
            raise MonitoringServiceError(
                "official view requires the same case_contract_id"
            )

        def summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            latencies = [float(row["artifact"]["latency_ms"]) for row in rows]
            return {
                "runs": list(rows),
                "valid_run_count": len(rows),
                "latency_median_ms": statistics.median(latencies),
                "latency_range_ms": [min(latencies), max(latencies)],
            }

        return {
            "case_contract_id": selected[0]["case_contract_id"],
            "baseline": summary(baseline),
            "candidate": summary(candidate),
        }


__all__ = [
    "MonitoringServiceError",
    "ReleaseScopedMonitoringService",
    "evaluate_typed_checks",
    "reproduction_seed_from_raw_report",
]
