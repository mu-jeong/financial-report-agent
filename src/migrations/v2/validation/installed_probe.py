"""Installed-process search probe for V2 installed-validation evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from src.migrations.v2.validation.performance import REQUIRED_WORKLOADS
from src.retrieval.reader import NativeRetrievalReader
from src.retrieval.repository import CatalogRepository


class InstalledProbeError(RuntimeError):
    """Raised when installed search behavior cannot satisfy the installed-validation contract."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one installed V2 installed-validation probe")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--query-spec", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args(argv)
    if args.samples < 2:
        parser.error("--samples must be at least 2")
    try:
        root = _safe_directory(args.data_root, "data root")
        specification = _read_specification(args.query_spec)
        result = run_probe(
            root,
            specification,
            samples=args.samples,
            query_spec_sha256=_sha256_file(args.query_spec.resolve(strict=True)),
        )
    except (OSError, ValueError, sqlite3.Error, InstalledProbeError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "kind": "v2_installed_validation_probe_failure",
                    "error": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


def run_probe(
    data_root: str | Path,
    specification: Mapping[str, Any],
    *,
    samples: int,
    query_spec_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 2:
        raise InstalledProbeError("probe samples must be an integer of at least 2")
    root = Path(data_root).resolve(strict=True)
    catalog = root / "catalog.sqlite3"
    if not catalog.is_file():
        catalog = root / "retrieval" / "v2" / "catalog.sqlite3"
    query = _validate_specification(specification)
    if query_spec_sha256 is None:
        query_spec_sha256 = hashlib.sha256(
            _canonical_json(dict(specification)).encode("utf-8")
        ).hexdigest()
    elif not _is_sha256(query_spec_sha256):
        raise InstalledProbeError("query specification hash is invalid")
    with CatalogRepository(catalog, data_root=root) as repository:
        with repository.request() as session:
            dimension = session.revision.descriptor.dimension
            initial_revision = session.revision
        vector = np.asarray(query["vector"], dtype=np.float32)
        if vector.shape != (dimension,) or not np.isfinite(vector).all():
            raise InstalledProbeError("query vector dimension or values are invalid")
        vector = np.ascontiguousarray(vector)
        reader = NativeRetrievalReader(repository)
        workloads: dict[str, Any] = {}
        for workload in REQUIRED_WORKLOADS:
            samples_payload: list[dict[str, Any]] = []
            for _ in range(samples):
                response = reader.search(
                    vector,
                    int(query["k"]),
                    query["scopes"][workload],
                )
                if response.revision != initial_revision:
                    raise InstalledProbeError("probe crossed a publication generation")
                if any(
                    result.snapshot_id != initial_revision.snapshot_id
                    or result.publication_generation
                    != initial_revision.publication_generation
                    for result in response.results
                ):
                    raise InstalledProbeError("probe hydrated a cross-generation result")
                timings = response.timings
                if timings is None:
                    raise InstalledProbeError("probe search did not emit timings")
                samples_payload.append(
                    {
                        "strategy": response.strategy.value,
                        "eligible_count": response.eligible_count,
                        "faiss_calls": response.faiss_calls,
                        "faiss_candidates": response.candidate_count,
                        "hydration_batches": response.hydration_batches,
                        "hydration_rows": response.hydration_rows,
                        "top_report_uid": (
                            response.results[0].report_uid if response.results else None
                        ),
                        "top_chunk_uid": (
                            response.results[0].chunk_uid if response.results else None
                        ),
                        "citation_complete": (
                            _citation_complete(response.results[0])
                            if response.results
                            else False
                        ),
                        "citation_sha256": (
                            _citation_sha256(response.results[0])
                            if response.results
                            else None
                        ),
                        "timings_ns": {
                            "scope_compile": timings.scope_compile_ns,
                            "eligibility": timings.eligibility_ns,
                            "faiss": timings.faiss_ns,
                            "hydration": timings.hydration_ns,
                            "lease": timings.lease_ns,
                            "total": timings.total_ns,
                        },
                    }
                )
            if workload == "empty" and any(
                item["faiss_calls"] != 0 or item["top_report_uid"] is not None
                for item in samples_payload
            ):
                raise InstalledProbeError("empty workload executed FAISS or returned results")
            workloads[workload] = {
                "samples": samples,
                "strategies": [item["strategy"] for item in samples_payload],
                "eligible_counts": [
                    item["eligible_count"] for item in samples_payload
                ],
                "faiss_calls": [item["faiss_calls"] for item in samples_payload],
                "faiss_candidates": [
                    item["faiss_candidates"] for item in samples_payload
                ],
                "hydration_batches": [
                    item["hydration_batches"] for item in samples_payload
                ],
                "hydration_rows": [
                    item["hydration_rows"] for item in samples_payload
                ],
                "top_report_uids": [
                    item["top_report_uid"] for item in samples_payload
                ],
                "top_chunk_uids": [
                    item["top_chunk_uid"] for item in samples_payload
                ],
                "citation_complete": [
                    item["citation_complete"] for item in samples_payload
                ],
                "citation_sha256": [
                    item["citation_sha256"] for item in samples_payload
                ],
                "timings_ns": [item["timings_ns"] for item in samples_payload],
            }

    expected_report_uid = str(query["expected_report_uid"])
    narrow = workloads["narrow"]
    top_report_uid = narrow["top_report_uids"][0]
    citation_complete = narrow["citation_complete"][0]
    expected_citation_sha256 = hashlib.sha256(
        _canonical_json(query["expected_citation"]).encode("utf-8")
    ).hexdigest()
    if (
        top_report_uid != expected_report_uid
        or any(uid != expected_report_uid for uid in narrow["top_report_uids"])
        or citation_complete is not True
        or any(value is not True for value in narrow["citation_complete"])
        or any(
            value != expected_citation_sha256
            for value in narrow["citation_sha256"]
        )
    ):
        raise InstalledProbeError("Gate D search/citation result is invalid")

    query_text = str(query["query_text"])
    return {
        "schema_version": 1,
        "kind": "v2_installed_validation_probe",
        "passed": True,
        "query_id": query["query_id"],
        "query_text_sha256": hashlib.sha256(query_text.encode("utf-8")).hexdigest(),
        "query_vector_sha256": hashlib.sha256(vector.tobytes()).hexdigest(),
        "query_spec_sha256": query_spec_sha256,
        "query_generation": {
            "provider": query["embedding_attestation"]["provider"],
            "model": query["embedding_attestation"]["model"],
            "input_type": query["embedding_attestation"]["input_type"],
            "provider_calls": query["embedding_attestation"]["provider_calls"],
            "attestation_sha256": hashlib.sha256(
                _canonical_json(query["embedding_attestation"]).encode("utf-8")
            ).hexdigest(),
        },
        "runtime_identity": {
            "active_snapshot_id": initial_revision.snapshot_id,
            "publication_generation": initial_revision.publication_generation,
            "active_build_id": initial_revision.build_id,
            "profile_id": initial_revision.profile_id,
            "snapshot_sha256": initial_revision.descriptor.sha256,
            "ntotal": initial_revision.descriptor.ntotal,
        },
        "workloads": workloads,
        "gate_d_search": {
            "expected_report_uid": expected_report_uid,
            "top_report_uid": top_report_uid,
            "top_rank": 1,
            "citation_complete": citation_complete,
            "citation_sha256": expected_citation_sha256,
        },
    }


def _validate_specification(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "query_id",
        "query_text",
        "vector",
        "embedding_attestation",
        "expected_report_uid",
        "expected_citation",
        "k",
        "scopes",
    }
    if set(value) != required or value.get("schema_version") != 1:
        raise InstalledProbeError("query specification fields are invalid")
    if value.get("kind") != "v2_release_semantic_query":
        raise InstalledProbeError("query specification kind is invalid")
    if not isinstance(value.get("query_id"), str) or not value["query_id"]:
        raise InstalledProbeError("query ID is invalid")
    if not isinstance(value.get("query_text"), str) or not value["query_text"].strip():
        raise InstalledProbeError("query text is invalid")
    vector = value.get("vector")
    if not isinstance(vector, list) or not vector:
        raise InstalledProbeError("query vector dimension or values are invalid")
    attestation = value.get("embedding_attestation")
    if not isinstance(attestation, Mapping) or set(attestation) != {
        "provider",
        "model",
        "input_type",
        "provider_calls",
        "query_text_sha256",
        "vector_sha256",
    }:
        raise InstalledProbeError("query embedding attestation is invalid")
    vector_sha256 = hashlib.sha256(
        np.asarray(vector, dtype=np.float32).tobytes()
    ).hexdigest()
    if (
        attestation.get("provider") != "openrouter"
        or not isinstance(attestation.get("model"), str)
        or not attestation.get("model")
        or attestation.get("input_type") != "search_query"
        or attestation.get("provider_calls") != 1
        or isinstance(attestation.get("provider_calls"), bool)
        or attestation.get("query_text_sha256")
        != hashlib.sha256(value["query_text"].encode("utf-8")).hexdigest()
        or attestation.get("vector_sha256") != vector_sha256
    ):
        raise InstalledProbeError("query embedding attestation is invalid")
    if any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not math.isfinite(float(item))
        for item in vector
    ):
        raise InstalledProbeError("query vector dimension or values are invalid")
    report_uid = value.get("expected_report_uid")
    if not _is_sha256(report_uid):
        raise InstalledProbeError("expected report UID is invalid")
    citation = value.get("expected_citation")
    citation_fields = {
        "canonical_relative_path",
        "report_type",
        "report_date",
        "target_name",
        "title",
        "broker",
    }
    if (
        not isinstance(citation, Mapping)
        or set(citation) != citation_fields
        or any(
            not isinstance(citation.get(field), str)
            or not citation.get(field).strip()
            for field in citation_fields
        )
    ):
        raise InstalledProbeError("expected citation is invalid")
    k = value.get("k")
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise InstalledProbeError("query k is invalid")
    scopes = value.get("scopes")
    if not isinstance(scopes, Mapping) or set(scopes) != set(REQUIRED_WORKLOADS):
        raise InstalledProbeError("query workloads are incomplete")
    if scopes.get("unfiltered") is not None:
        raise InstalledProbeError("unfiltered workload must use null scope")
    if not isinstance(scopes.get("empty"), Mapping) or scopes["empty"].get(
        "empty"
    ) is not True:
        raise InstalledProbeError("empty workload is invalid")
    if any(
        not isinstance(scopes[name], Mapping) or not scopes[name]
        for name in REQUIRED_WORKLOADS
        if name not in {"unfiltered", "empty"}
    ):
        raise InstalledProbeError("filtered query workloads are invalid")
    return dict(value)


def _citation_complete(result: Any) -> bool:
    return all(
        isinstance(value, str) and bool(value.strip())
        for value in (
            result.canonical_relative_path,
            result.report_date,
            result.title,
            result.broker,
            result.report_type,
        )
    )


def _citation_sha256(result: Any) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "canonical_relative_path": result.canonical_relative_path,
                "report_type": result.report_type,
                "report_date": result.report_date,
                "target_name": result.target_name,
                "title": result.title,
                "broker": result.broker,
            }
        ).encode("utf-8")
    ).hexdigest()


def _read_specification(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise InstalledProbeError("query specification path is unsafe")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise InstalledProbeError("query specification path is unsafe")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstalledProbeError("query specification is unreadable") from exc
    if not isinstance(value, dict):
        raise InstalledProbeError("query specification must be an object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or path.is_symlink():
        raise InstalledProbeError(f"{label} is unsafe")
    return resolved


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


if __name__ == "__main__":
    sys.exit(main())
