"""Produce immutable, redacted Gate C evidence for a copied V1/V2 seed."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.migrations.v2.reconstruct import (  # noqa: E402
    render_embedding_prefix,
    strip_embedding_prefix,
)
from src.retrieval.bootstrap import (  # noqa: E402
    inspect_runtime,
    resolve_epoch_zero_compatibility_bundle_id,
)
from src.migrations.v2.compatibility import (  # noqa: E402
    LegacySearchResult,
    V1CompatibilityReader,
)
from src.migrations.v2.validation.copied_install_benchmark import _metadata_predicate  # noqa: E402
from src.retrieval.identity import canonical_json  # noqa: E402
from src.migrations.v2.validation.performance import REQUIRED_WORKLOADS  # noqa: E402
from src.retrieval.reader import NativeRetrievalReader  # noqa: E402
from src.retrieval.repository import (  # noqa: E402
    CatalogRepository,
    RetrievedChunk,
    compile_scope_filters,
)


GATE_C_WORKLOADS = (
    "unfiltered",
    "company",
    "report_type",
    "date",
    "narrow",
    "broad",
    "empty",
    "prior_scope",
)
MISMATCH_FIELDS = (
    "eligible_set",
    "top_k_logical_chunks",
    "top_k_sources",
    "top_k_bodies",
    "top_k_scores",
    "top_k_ranks",
    "citations",
    "snapshot_generation",
)
_HEX = frozenset("0123456789abcdef")


class ReaderParityError(ValueError):
    """Raised when parity evidence cannot be established without guessing."""


@dataclass(frozen=True)
class _MappingRow:
    chunk_uid: str
    faiss_id: int
    legacy_document_id: str
    legacy_ordinal: int
    parent_uid: str


@dataclass(frozen=True)
class _CitationSource:
    report_uid: str
    canonical_relative_path: str
    source_sha256: str


@dataclass(frozen=True)
class _ParityHit:
    chunk_uid: str
    parent_uid: str
    score: float
    body: str
    source: tuple[str, str, str, str | None, str, str]
    citation_source: _CitationSource


@dataclass(frozen=True)
class _SeedEvidence:
    snapshot_id: str
    snapshot_sha256: str
    publication_generation: int
    write_epoch: int
    dimension: int
    metric: str
    ntotal: int
    prefix_template: str
    manifest_sha256: str
    mapping_sha256: str
    source_manifest_sha256: str
    mapping_by_ordinal: dict[int, _MappingRow]
    citation_by_chunk: dict[str, _CitationSource]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare the sealed epoch-zero V1 bridge with its native V2 reader "
            "and write immutable redacted Gate C evidence"
        )
    )
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--query-input", required=True, type=Path)
    parser.add_argument("--scope-input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    _validate_output(args.output)
    data_root = _plain_directory(args.data_root)
    query_bytes, query_input = _read_json(args.query_input, "query input")
    scope_bytes, scope_input = _read_json(args.scope_input, "scope input")
    selection = inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=True,
    )
    if (
        not selection.is_native
        or selection.write_epoch != 0
        or not selection.v1_fallback_open
        or not selection.active_snapshot_id
    ):
        raise ReaderParityError(
            "reader parity requires a validated epoch-zero native seed with V1 open"
        )
    bundle_id = resolve_epoch_zero_compatibility_bundle_id(selection)
    seed = _load_seed_evidence(data_root, selection, bundle_id)
    vectors, k = _load_queries(
        query_input,
        dimension=seed.dimension,
        ntotal=seed.ntotal,
    )
    scopes = _load_scopes(scope_input)

    legacy = V1CompatibilityReader(data_root, bundle_id)
    repository = CatalogRepository(
        data_root / "retrieval" / "v2" / "catalog.sqlite3",
        data_root=data_root,
    )
    native = NativeRetrievalReader(repository)
    workloads: dict[str, dict[str, Any]] = {}
    aggregate = _zero_mismatches()
    totals = {
        "legacy_eligible_count": 0,
        "native_eligible_count": 0,
        "legacy_exact_score_tie_groups": 0,
        "native_exact_score_tie_groups": 0,
    }
    try:
        if (legacy.dimension, legacy.metric, legacy.ntotal) != (
            seed.dimension,
            seed.metric,
            seed.ntotal,
        ):
            raise ReaderParityError("V1 and V2 do not share one corpus space")
        for workload in GATE_C_WORKLOADS:
            mismatches = _zero_mismatches()
            counts = {field: 0 for field in totals}
            for vector in vectors:
                request = _compare_request(
                    vector,
                    k=k,
                    scope=scopes[workload],
                    seed=seed,
                    legacy=legacy,
                    native=native,
                )
                _add(mismatches, request["mismatches"])
                for field in counts:
                    counts[field] += request[field]
            if workload == "empty" and (
                counts["legacy_eligible_count"]
                or counts["native_eligible_count"]
            ):
                mismatches["eligible_set"] += max(
                    counts["legacy_eligible_count"],
                    counts["native_eligible_count"],
                    1,
                )
            workloads[workload] = {
                "passed": not any(mismatches.values()),
                "request_count": len(vectors),
                **counts,
                "mismatches": mismatches,
            }
            _add(aggregate, mismatches)
            for field in totals:
                totals[field] += counts[field]
    finally:
        legacy.close()
        repository.close()
        repository.cache.close()

    passed = not any(aggregate.values())
    payload = {
        "schema_version": 1,
        "kind": "v1_v2_copied_install_reader_parity",
        "status": "passed" if passed else "failed",
        "inputs": {
            "query_input_sha256": hashlib.sha256(query_bytes).hexdigest(),
            "scope_input_sha256": hashlib.sha256(scope_bytes).hexdigest(),
        },
        "runtime": {
            "snapshot_id": seed.snapshot_id,
            "snapshot_sha256": seed.snapshot_sha256,
            "publication_generation": seed.publication_generation,
            "write_epoch": seed.write_epoch,
            "dimension": seed.dimension,
            "metric": seed.metric,
            "ntotal": seed.ntotal,
            "conversion_manifest_sha256": seed.manifest_sha256,
            "legacy_mapping_sha256": seed.mapping_sha256,
            "source_manifest_sha256": seed.source_manifest_sha256,
        },
        "protocol": {
            "query_count": len(vectors),
            "workload_count": len(GATE_C_WORKLOADS),
            "request_count": len(vectors) * len(GATE_C_WORKLOADS),
            "k": k,
            "exact_score_tie_policy": "score_group_then_chunk_uid_before_top_k",
            "l2_order": "ascending_score_then_chunk_uid_for_exact_ties",
            "inner_product_order": "descending_score_then_chunk_uid_for_exact_ties",
        },
        "counts": totals,
        "workloads": workloads,
        "mismatches": aggregate,
    }
    _write_once(args.output, payload)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "request_count": payload["protocol"]["request_count"],
                "mismatch_count": sum(aggregate.values()),
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


def _compare_request(
    vector: np.ndarray,
    *,
    k: int,
    scope: dict[str, Any] | None,
    seed: _SeedEvidence,
    legacy: V1CompatibilityReader,
    native: NativeRetrievalReader,
) -> dict[str, Any]:
    legacy_all = legacy.search(vector, k=seed.ntotal, fetch_k=seed.ntotal)
    if len(legacy_all) != seed.ntotal:
        raise ReaderParityError("V1 full-corpus search did not return N rows")
    predicate = _metadata_predicate(scope)
    if predicate is not None:
        legacy_all = [item for item in legacy_all if predicate(item.metadata)]
    legacy_hits = tuple(
        _legacy_hit(item, seed.mapping_by_ordinal[item.legacy_ordinal], seed)
        for item in legacy_all
    )

    response = native.search(vector, seed.ntotal, scope=scope)
    native_hits = tuple(_native_hit(item) for item in response.results)
    mismatches = _zero_mismatches()
    if response.eligible_count != len(native_hits):
        mismatches["eligible_set"] += (
            abs(response.eligible_count - len(native_hits)) or 1
        )
    mismatches["snapshot_generation"] += sum(
        1
        for item in response.results
        if (
            item.snapshot_id != seed.snapshot_id
            or item.publication_generation != seed.publication_generation
            or item.snapshot_id != response.revision.snapshot_id
            or item.publication_generation != response.revision.publication_generation
        )
    )
    if (
        response.revision.snapshot_id != seed.snapshot_id
        or response.revision.publication_generation != seed.publication_generation
    ):
        mismatches["snapshot_generation"] += 1

    legacy_hits, legacy_ties = _canonicalize_exact_ties(legacy_hits, seed.metric)
    native_hits, native_ties = _canonicalize_exact_ties(native_hits, seed.metric)
    _add(mismatches, _compare_hits(legacy_hits, native_hits, k=k))
    return {
        "legacy_eligible_count": len(legacy_hits),
        "native_eligible_count": len(native_hits),
        "legacy_exact_score_tie_groups": legacy_ties,
        "native_exact_score_tie_groups": native_ties,
        "mismatches": mismatches,
    }


def _legacy_hit(
    result: LegacySearchResult,
    mapping: _MappingRow,
    seed: _SeedEvidence,
) -> _ParityHit:
    try:
        prefix = render_embedding_prefix(seed.prefix_template, result.metadata)
        body = strip_embedding_prefix(result.embedding_text, prefix)
        citation = seed.citation_by_chunk[mapping.chunk_uid]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReaderParityError("V1 result cannot map to native identity") from exc
    return _ParityHit(
        mapping.chunk_uid,
        mapping.parent_uid,
        result.score,
        body,
        _source_tuple(result.metadata),
        citation,
    )


def _native_hit(result: RetrievedChunk) -> _ParityHit:
    return _ParityHit(
        result.chunk_uid,
        result.parent_uid,
        result.score,
        result.parent_slice,
        (
            result.file_name,
            result.report_type,
            result.report_date,
            result.target_name,
            result.title,
            result.broker,
        ),
        _CitationSource(
            result.report_uid,
            result.canonical_relative_path,
            result.source_sha256,
        ),
    )


def _source_tuple(metadata: dict[str, Any]) -> tuple[str, str, str, str | None, str, str]:
    required = ("file_name", "report_type", "report_date", "title", "broker")
    if any(not isinstance(metadata.get(field), str) for field in required):
        raise ReaderParityError("V1 source metadata is incomplete")
    target = metadata.get("target_name")
    if target is not None and not isinstance(target, str):
        raise ReaderParityError("V1 target metadata is invalid")
    return (
        metadata["file_name"],
        metadata["report_type"],
        metadata["report_date"],
        target,
        metadata["title"],
        metadata["broker"],
    )


def _canonicalize_exact_ties(
    hits: tuple[_ParityHit, ...],
    metric: str,
) -> tuple[tuple[_ParityHit, ...], int]:
    if metric not in {"l2", "inner_product"}:
        raise ReaderParityError("parity metric is unsupported")
    for previous, current in zip(hits, hits[1:]):
        inverted = (
            current.score < previous.score
            if metric == "l2"
            else current.score > previous.score
        )
        if inverted:
            raise ReaderParityError("reader returned non-monotonic unequal scores")
    output: list[_ParityHit] = []
    tie_groups = 0
    start = 0
    while start < len(hits):
        end = start + 1
        while end < len(hits) and hits[end].score == hits[start].score:
            end += 1
        group = hits[start:end]
        if len(group) > 1:
            tie_groups += 1
            group = tuple(sorted(group, key=lambda hit: hit.chunk_uid))
        output.extend(group)
        start = end
    return tuple(output), tie_groups


def _compare_hits(
    legacy: tuple[_ParityHit, ...],
    native: tuple[_ParityHit, ...],
    *,
    k: int,
) -> dict[str, int]:
    mismatches = _zero_mismatches()
    mismatches["eligible_set"] = len(
        {item.chunk_uid for item in legacy}
        ^ {item.chunk_uid for item in native}
    )
    left, right = legacy[:k], native[:k]
    for field, projection in (
        ("top_k_logical_chunks", lambda hit: (hit.chunk_uid, hit.parent_uid)),
        ("top_k_sources", lambda hit: hit.source),
        ("top_k_bodies", lambda hit: hit.body),
        ("top_k_scores", lambda hit: hit.score),
    ):
        mismatches[field] = _position_mismatches(left, right, projection)
    left_ranks = {item.chunk_uid: rank for rank, item in enumerate(left, 1)}
    right_ranks = {item.chunk_uid: rank for rank, item in enumerate(right, 1)}
    mismatches["top_k_ranks"] = sum(
        left_ranks.get(uid) != right_ranks.get(uid)
        for uid in left_ranks.keys() | right_ranks.keys()
    )
    mismatches["citations"] = _position_mismatches(
        tuple(enumerate(left, 1)),
        tuple(enumerate(right, 1)),
        lambda ranked: (ranked[0], ranked[1].citation_source),
    ) + sum(not _complete_citation(item.citation_source) for item in right)
    return mismatches


def _position_mismatches(
    left: tuple[Any, ...],
    right: tuple[Any, ...],
    projection: Callable[[Any], object],
) -> int:
    return abs(len(left) - len(right)) + sum(
        projection(a) != projection(b) for a, b in zip(left, right)
    )


def _load_queries(
    value: Any,
    *,
    dimension: int,
    ntotal: int,
) -> tuple[tuple[np.ndarray, ...], int]:
    value = _object(
        value,
        {"schema_version", "kind", "k", "queries", "workloads"},
        "query-vector input",
    )
    if value["schema_version"] != 1 or value["kind"] != "v2_retrieval_query_vectors":
        raise ReaderParityError("query-vector input kind is invalid")
    k = value["k"]
    if isinstance(k, bool) or not isinstance(k, int) or not 0 < k <= ntotal:
        raise ReaderParityError("query-vector k must be in the copied corpus")
    if not isinstance(value["queries"], list) or not value["queries"]:
        raise ReaderParityError("query-vector input needs at least one query")
    vectors: list[np.ndarray] = []
    query_ids: set[str] = set()
    for raw in value["queries"]:
        raw = _object(raw, {"query_id", "vector"}, "query-vector row")
        query_id = raw["query_id"]
        if not isinstance(query_id, str) or not query_id or query_id in query_ids:
            raise ReaderParityError("query IDs must be unique non-empty strings")
        vector = np.asarray(raw["vector"], dtype=np.float32)
        if vector.shape != (dimension,) or not np.isfinite(vector).all():
            raise ReaderParityError("query vector shape or values are invalid")
        query_ids.add(query_id)
        vectors.append(np.ascontiguousarray(vector))
    workloads = value["workloads"]
    if not isinstance(workloads, dict) or set(workloads) != set(REQUIRED_WORKLOADS):
        raise ReaderParityError("benchmark workloads are incomplete")
    for name in REQUIRED_WORKLOADS:
        definition = _object(workloads[name], {"scope"}, "benchmark workload")
        scope = definition["scope"]
        if scope is not None and not isinstance(scope, dict):
            raise ReaderParityError("benchmark scope is invalid")
        compiled = compile_scope_filters(scope)
        if name == "unfiltered" and not compiled.is_unfiltered:
            raise ReaderParityError("benchmark unfiltered scope must be null")
        if name != "unfiltered" and (scope is None or compiled.is_unfiltered):
            raise ReaderParityError("benchmark filtered scope must be non-empty")
    return tuple(vectors), k


def _load_scopes(value: Any) -> dict[str, dict[str, Any] | None]:
    value = _object(value, {"schema_version", "kind", "workloads"}, "scope input")
    if value["schema_version"] != 1 or value["kind"] != "v2_reader_parity_scopes":
        raise ReaderParityError("Gate C scope input kind is invalid")
    workloads = value["workloads"]
    if not isinstance(workloads, dict) or set(workloads) != set(GATE_C_WORKLOADS):
        raise ReaderParityError("Gate C workload names are incomplete")
    scopes: dict[str, dict[str, Any] | None] = {}
    for name in GATE_C_WORKLOADS:
        scope = _object(workloads[name], {"scope"}, "Gate C workload")["scope"]
        if scope is not None and not isinstance(scope, dict):
            raise ReaderParityError("Gate C scope must be an object or null")
        compiled = compile_scope_filters(scope)
        if name == "unfiltered" and (scope is not None or not compiled.is_unfiltered):
            raise ReaderParityError("Gate C unfiltered scope must be null")
        if name != "unfiltered" and (scope is None or compiled.is_unfiltered):
            raise ReaderParityError("Gate C filtered scope must be non-empty")
        scopes[name] = scope
    named_fields = {
        "company": {"company", "company_name", "target", "target_name"},
        "report_type": {"report_type", "report_types", "company", "company_name"},
        "date": {
            "date",
            "report_date",
            "date_start",
            "date_end",
            "report_date_start",
            "report_date_end",
        },
    }
    for name, fields in named_fields.items():
        assert scopes[name] is not None
        if not fields.intersection(scopes[name] or {}):
            raise ReaderParityError(f"Gate C {name} scope does not exercise its field")
    if "prior_scope" not in (scopes["prior_scope"] or {}):
        raise ReaderParityError("Gate C prior_scope is missing")
    return scopes


def _load_seed_evidence(data_root: Path, selection: Any, bundle_id: str) -> _SeedEvidence:
    connection = _open_read_only(data_root / "retrieval" / "v2" / "catalog.sqlite3")
    try:
        rows = connection.execute(
            """
            SELECT runtime.active_snapshot_id AS snapshot_id,
                   runtime.active_build_id AS build_id,
                   runtime.publication_generation, runtime.write_epoch,
                   snapshot.file_sha256 AS snapshot_sha256,
                   snapshot.relative_path AS snapshot_path,
                   snapshot.size_bytes, snapshot.dimension, snapshot.metric,
                   snapshot.ntotal, build.source_manifest_json,
                   build.source_manifest_sha256, profile.profile_hash,
                   profile.prefix_template, publication.publication_id,
                   publication.evidence_manifest_relative_path AS manifest_path,
                   publication.evidence_manifest_sha256 AS manifest_sha256
            FROM retrieval_runtime AS runtime
            JOIN vector_snapshots AS snapshot
              ON snapshot.snapshot_id = runtime.active_snapshot_id
             AND snapshot.build_id = runtime.active_build_id
             AND snapshot.state = 'ready'
            JOIN retrieval_builds AS build
              ON build.build_id = runtime.active_build_id
             AND build.state = 'fully_complete'
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            JOIN publication_runs AS publication
              ON publication.to_snapshot_id = runtime.active_snapshot_id
             AND publication.state = 'fully_complete'
            WHERE runtime.runtime_id = 1
            """
        ).fetchall()
        if len(rows) != 1:
            raise ReaderParityError("seed requires one sealed conversion publication")
        row = dict(rows[0])
        source_json = str(row["source_manifest_json"])
        source_hash = str(row["source_manifest_sha256"])
        if _sha256(source_json.encode("utf-8")) != source_hash:
            raise ReaderParityError("source manifest hash is invalid")
        manifest_bytes, manifest = _read_sealed(
            data_root,
            str(row["manifest_path"]),
            str(row["manifest_sha256"]),
            "conversion manifest",
        )
        counts = connection.execute(
            """SELECT (SELECT COUNT(*) FROM reports),
                      (SELECT COUNT(*) FROM retrieval_parents),
                      (SELECT COUNT(*) FROM retrieval_chunks)"""
        ).fetchone()
        _validate_conversion_manifest(
            manifest,
            row=row,
            bundle_id=bundle_id,
            source_hash=source_hash,
            counts=tuple(int(value) for value in counts),
        )
        mapping_descriptor = manifest["legacy_mapping"]
        mapping_bytes, mapping = _read_sealed(
            data_root,
            mapping_descriptor["relative_path"],
            mapping_descriptor["sha256"],
            "legacy mapping",
        )
        mapping_by_ordinal = _validate_mapping(
            mapping,
            manifest=manifest,
            ntotal=int(row["ntotal"]),
        )
        citations = _validate_catalog_mapping(
            connection,
            snapshot_id=str(row["snapshot_id"]),
            mapping=mapping_by_ordinal,
        )
    finally:
        connection.close()
    if (
        row["snapshot_id"] != selection.active_snapshot_id
        or int(row["publication_generation"]) != selection.publication_generation
        or int(row["write_epoch"]) != selection.write_epoch
    ):
        raise ReaderParityError("seed changed during evidence validation")
    return _SeedEvidence(
        snapshot_id=str(row["snapshot_id"]),
        snapshot_sha256=str(row["snapshot_sha256"]),
        publication_generation=int(row["publication_generation"]),
        write_epoch=int(row["write_epoch"]),
        dimension=int(row["dimension"]),
        metric=str(row["metric"]),
        ntotal=int(row["ntotal"]),
        prefix_template=str(row["prefix_template"]),
        manifest_sha256=_sha256(manifest_bytes),
        mapping_sha256=_sha256(mapping_bytes),
        source_manifest_sha256=source_hash,
        mapping_by_ordinal=mapping_by_ordinal,
        citation_by_chunk=citations,
    )


def _validate_conversion_manifest(
    manifest: Any,
    *,
    row: dict[str, Any],
    bundle_id: str,
    source_hash: str,
    counts: tuple[int, int, int],
) -> None:
    manifest = _object(
        manifest,
        {
            "schema_version",
            "publication_id",
            "build_id",
            "snapshot_id",
            "profile_hash",
            "compatibility_bundle_id",
            "assessment_digest",
            "reconstruction_digest",
            "source_manifest_sha256",
            "snapshot",
            "counts",
            "legacy_mapping",
            "vector_max_absolute_error",
            "prohibited_conversion_calls",
        },
        "conversion manifest",
    )
    identity = (
        manifest["schema_version"],
        manifest["publication_id"],
        manifest["build_id"],
        manifest["snapshot_id"],
        manifest["profile_hash"],
        manifest["compatibility_bundle_id"],
        manifest["source_manifest_sha256"],
    )
    expected = (
        1,
        row["publication_id"],
        row["build_id"],
        row["snapshot_id"],
        row["profile_hash"],
        bundle_id,
        source_hash,
    )
    if identity != expected:
        raise ReaderParityError("conversion manifest identity is invalid")
    expected_snapshot = {
        "relative_path": row["snapshot_path"],
        "sha256": row["snapshot_sha256"],
        "size_bytes": int(row["size_bytes"]),
        "dimension": int(row["dimension"]),
        "metric": row["metric"],
        "ntotal": int(row["ntotal"]),
    }
    if manifest["snapshot"] != expected_snapshot:
        raise ReaderParityError("conversion snapshot descriptor is invalid")
    expected_counts = dict(zip(("reports", "parents", "chunks"), counts))
    if manifest["counts"] != expected_counts or counts[2] != int(row["ntotal"]):
        raise ReaderParityError("conversion counts are invalid")
    if not all(_digest(manifest[field]) for field in (
        "assessment_digest",
        "reconstruction_digest",
    )):
        raise ReaderParityError("conversion reconstruction digest is invalid")
    mapping = _object(
        manifest["legacy_mapping"],
        {"relative_path", "sha256"},
        "legacy mapping descriptor",
    )
    if not isinstance(mapping["relative_path"], str) or not _digest(mapping["sha256"]):
        raise ReaderParityError("legacy mapping descriptor is invalid")
    maximum_error = manifest["vector_max_absolute_error"]
    if (
        isinstance(maximum_error, bool)
        or not isinstance(maximum_error, (int, float))
        or not np.isfinite(maximum_error)
        or not 0 <= float(maximum_error) <= 1e-6
    ):
        raise ReaderParityError("conversion vector parity is invalid")
    prohibited = manifest["prohibited_conversion_calls"]
    if not isinstance(prohibited, dict) or set(prohibited) != {
        "api",
        "chunking",
        "crawler",
        "embedding",
        "extraction",
        "network",
        "pdf_reads",
    } or any(value != 0 for value in prohibited.values()):
        raise ReaderParityError("conversion prohibited-call evidence is invalid")


def _validate_mapping(
    value: Any,
    *,
    manifest: dict[str, Any],
    ntotal: int,
) -> dict[int, _MappingRow]:
    value = _object(
        value,
        {"schema_version", "assessment_digest", "reconstruction_digest", "rows"},
        "legacy mapping",
    )
    if (
        value["schema_version"] != 1
        or value["assessment_digest"] != manifest["assessment_digest"]
        or value["reconstruction_digest"] != manifest["reconstruction_digest"]
        or not isinstance(value["rows"], list)
        or len(value["rows"]) != ntotal
    ):
        raise ReaderParityError("legacy mapping identity or count is invalid")
    rows: dict[int, _MappingRow] = {}
    for raw in value["rows"]:
        raw = _object(
            raw,
            {
                "chunk_uid",
                "faiss_id",
                "legacy_document_id",
                "legacy_ordinal",
                "parent_uid",
            },
            "legacy mapping row",
        )
        item = _MappingRow(**raw)
        if (
            isinstance(item.faiss_id, bool)
            or not isinstance(item.faiss_id, int)
            or isinstance(item.legacy_ordinal, bool)
            or not isinstance(item.legacy_ordinal, int)
            or not _digest(item.chunk_uid)
            or not _digest(item.parent_uid)
            or not isinstance(item.legacy_document_id, str)
            or not item.legacy_document_id
            or item.legacy_ordinal in rows
        ):
            raise ReaderParityError("legacy mapping row is invalid")
        rows[item.legacy_ordinal] = item
    if (
        set(rows) != set(range(ntotal))
        or {item.faiss_id for item in rows.values()} != set(range(1, ntotal + 1))
        or len({item.chunk_uid for item in rows.values()}) != ntotal
        or len({item.legacy_document_id for item in rows.values()}) != ntotal
    ):
        raise ReaderParityError("legacy mapping is not a full-N bijection")
    return rows


def _validate_catalog_mapping(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    mapping: dict[int, _MappingRow],
) -> dict[str, _CitationSource]:
    rows = connection.execute(
        """
        SELECT membership.chunk_uid, membership.faiss_id, chunk.parent_uid,
               report.report_uid, report.canonical_relative_path,
               report.source_sha256
        FROM snapshot_membership AS membership
        JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
        JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
        JOIN reports AS report ON report.report_id = parent.report_id
        WHERE membership.snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchall()
    expected = {
        item.chunk_uid: (item.faiss_id, item.parent_uid)
        for item in mapping.values()
    }
    observed = {str(row[0]): (int(row[1]), str(row[2])) for row in rows}
    if observed != expected:
        raise ReaderParityError("legacy mapping does not match native membership")
    citations = {
        str(row[0]): _CitationSource(str(row[3]), str(row[4]), str(row[5]))
        for row in rows
    }
    if not all(_complete_citation(value) for value in citations.values()):
        raise ReaderParityError("native citation metadata is incomplete")
    return citations


def _read_sealed(
    root: Path,
    relative: str,
    expected_hash: str,
    label: str,
) -> tuple[bytes, Any]:
    path = _anchored_file(root, relative, label)
    raw, value = _read_json(path, label, reject_symlink=False)
    if _sha256(raw) != expected_hash or _writable(path):
        raise ReaderParityError(f"{label} is unsealed or hash-invalid")
    return raw, value


def _read_json(
    path: Path,
    label: str,
    *,
    reject_symlink: bool = True,
) -> tuple[bytes, Any]:
    source = Path(path)
    if (reject_symlink and source.is_symlink()) or not source.is_file():
        raise ReaderParityError(f"{label} must be a plain file")
    try:
        raw = source.read_bytes()
        return raw, json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReaderParityError(f"{label} JSON is unreadable") from exc


def _plain_directory(path: Path) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ReaderParityError("data root must be a plain directory")
    return candidate.resolve(strict=True)


def _anchored_file(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ReaderParityError(f"{label} path is invalid")
    pure = PurePosixPath(relative.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts:
        raise ReaderParityError(f"{label} path escapes the data root")
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise ReaderParityError(f"{label} path contains a symlink")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReaderParityError(f"{label} path is unavailable") from exc
    if not resolved.is_file():
        raise ReaderParityError(f"{label} is not a file")
    return resolved


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(path.resolve(strict=True).as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _validate_output(path: Path) -> None:
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise ReaderParityError("parity output already exists")
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise ReaderParityError("parity output parent must be a plain directory")


def _write_once(path: Path, value: dict[str, Any]) -> None:
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    try:
        with Path(path).open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        Path(path).chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except FileExistsError as exc:
        raise ReaderParityError("parity output already exists") from exc


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ReaderParityError(f"{label} fields are invalid")
    return value


def _complete_citation(value: _CitationSource) -> bool:
    path = PurePosixPath(value.canonical_relative_path)
    return (
        _digest(value.report_uid)
        and bool(value.canonical_relative_path)
        and not path.is_absolute()
        and ".." not in path.parts
        and _digest(value.source_sha256)
    )


def _zero_mismatches() -> dict[str, int]:
    return {field: 0 for field in MISMATCH_FIELDS}


def _add(target: dict[str, int], values: dict[str, int]) -> None:
    for field in MISMATCH_FIELDS:
        target[field] += values[field]


def _writable(path: Path) -> bool:
    return bool(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not (set(value) - _HEX)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
