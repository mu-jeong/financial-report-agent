"""Real copied-install adapter for the fresh-process retrieval benchmark CLI.

The adapter is configured only through two environment variables so child
processes reopen both readers independently:

``V2_BENCHMARK_DATA_ROOT`` points at the converted epoch-zero data root and
``V2_BENCHMARK_INPUT`` points at a query-vector/workload JSON file.  Neither
path nor any vector/query payload is copied into release evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import tempfile
import time
import weakref
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import faiss
import numpy as np

from src.migrations.v2.validation.benchmark_runner import (
    BenchmarkFactory,
    FixedBenchmarkQuery,
    ProbeTelemetry,
)
from src.retrieval.bootstrap import (
    inspect_runtime,
    resolve_epoch_zero_compatibility_bundle_id,
)
from src.migrations.v2.compatibility import V1CompatibilityReader
from src.retrieval.dispatch import prime_native_dispatch
from src.migrations.v2.validation.performance import REQUIRED_WORKLOADS, PerformanceEvidenceError
from src.retrieval.reader import NativeRetrievalReader
from src.retrieval.repository import (
    CatalogRepository,
    SnapshotRevision,
    compile_scope_filters,
)
from src.retrieval.schema import (
    SchemaError,
    checkpoint_isolated_catalog,
    configure_catalog_storage,
)
from src.retrieval.vector_index import SnapshotDescriptor


@dataclass(frozen=True)
class _QueryPayload:
    vector: np.ndarray
    scope: dict[str, Any] | None
    k: int
    v1_fetch_k: int


def create_factory(
    *,
    process_id: str,
    seed: int,
    engine: str = "paired",
) -> BenchmarkFactory:
    """Open independent V1/V2 readers for one fresh benchmark process."""

    del process_id, seed
    if engine not in {"paired", "v1", "v2"}:
        raise PerformanceEvidenceError("copied benchmark engine is invalid")
    data_root, input_bytes, specification = _benchmark_inputs()

    selection = inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=engine != "v1",
    )
    if (
        not selection.is_native
        or selection.write_epoch != 0
        or not selection.v1_fallback_open
        or not selection.active_snapshot_id
    ):
        raise PerformanceEvidenceError(
            "copied-install benchmark requires a validated epoch-zero bridge"
        )
    descriptor = _active_descriptor(selection.paths.catalog, selection.active_snapshot_id)
    v1_reader = None
    if engine in {"paired", "v1"}:
        bundle_id = resolve_epoch_zero_compatibility_bundle_id(selection)
        v1_reader = V1CompatibilityReader(data_root, bundle_id)
    v2_reader = None
    if engine in {"paired", "v2"}:
        # Use the same process-scoped reader holder as production dispatch so
        # warm measurements include request work, not per-query construction.
        v2_reader = prime_native_dispatch(selection).reader
    if v1_reader is not None and (
        v1_reader.dimension != descriptor["dimension"]
        or v1_reader.metric != descriptor["metric"]
        or v1_reader.ntotal != descriptor["ntotal"]
    ):
        raise PerformanceEvidenceError(
            "V1 and V2 benchmark descriptors do not share one corpus space"
        )
    queries, k = _load_queries(
        specification,
        dimension=descriptor["dimension"],
        v1_ntotal=descriptor["ntotal"],
    )

    def v1_probe(workload: str, payload: _QueryPayload) -> ProbeTelemetry:
        if v1_reader is None:
            raise PerformanceEvidenceError("V1 probe is unavailable in this worker")
        predicate = _metadata_predicate(payload.scope)
        started = time.perf_counter_ns()
        candidates = v1_reader.search(
            payload.vector,
            k=payload.v1_fetch_k,
            fetch_k=payload.v1_fetch_k,
        )
        if predicate is None:
            results = candidates[: payload.k]
        else:
            results = [
                result for result in candidates if predicate(result.metadata)
            ][: payload.k]
        elapsed = time.perf_counter_ns() - started
        return ProbeTelemetry(
            sql_ns=0,
            sql_rows=0,
            strategy="compatibility",
            faiss_ns=elapsed,
            faiss_calls=1,
            faiss_candidates=min(payload.v1_fetch_k, v1_reader.ntotal),
            hydration_batches=int(bool(candidates)),
            hydration_rows=len(candidates),
            rerank_ns=0,
            lease_ns=0,
        )

    def v2_probe(workload: str, payload: _QueryPayload) -> ProbeTelemetry:
        del workload
        if v2_reader is None:
            raise PerformanceEvidenceError("V2 probe is unavailable in this worker")
        return _native_probe(v2_reader, payload)

    return BenchmarkFactory(
        queries=queries,
        v1_probe=v1_probe,
        v2_probe=v2_probe,
        environment={
            "os": platform.system().lower(),
            "os_release": platform.release(),
            "python_version": platform.python_version(),
            "faiss_version": getattr(faiss, "__version__", "unknown"),
            "numpy_version": np.__version__,
            "cache_state": "warm",
            "reranker": "disabled-reader-parity",
            "query_input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "active_snapshot_id": selection.active_snapshot_id,
            "active_snapshot_sha256": descriptor["sha256"],
            "dimension": descriptor["dimension"],
            "metric": descriptor["metric"],
            "ntotal": descriptor["ntotal"],
            "k": k,
        },
        engine=engine,
    )


def create_successor_factory(
    *,
    process_id: str,
    seed: int,
    engine: str = "paired",
) -> BenchmarkFactory:
    """Pair a pinned native predecessor with the live native successor."""

    del process_id, seed
    if engine not in {"paired", "v1", "v2"}:
        raise PerformanceEvidenceError("copied benchmark engine is invalid")
    data_root, input_bytes, specification = _benchmark_inputs()
    selection = inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=True,
    )
    if (
        not selection.is_native
        or selection.write_epoch <= 0
        or selection.v1_fallback_open
        or selection.degraded
        or not selection.write_enabled
        or not selection.active_snapshot_id
        or not selection.predecessor_snapshot_id
        or selection.active_snapshot_id == selection.predecessor_snapshot_id
    ):
        raise PerformanceEvidenceError(
            "successor benchmark requires a healthy native successor and predecessor"
        )
    active_descriptor = _active_descriptor(
        selection.paths.catalog,
        selection.active_snapshot_id,
    )
    predecessor_descriptor = _active_descriptor(
        selection.paths.catalog,
        selection.predecessor_snapshot_id,
    )
    if (
        active_descriptor["dimension"] != predecessor_descriptor["dimension"]
        or active_descriptor["metric"] != predecessor_descriptor["metric"]
    ):
        raise PerformanceEvidenceError(
            "successor and predecessor do not share one vector space"
        )
    queries, k = _load_queries(
        specification,
        dimension=active_descriptor["dimension"],
        v1_ntotal=min(
            active_descriptor["ntotal"],
            predecessor_descriptor["ntotal"],
        ),
    )
    pinned_pair = _pinned_snapshot_pair(
        selection.paths.catalog,
        data_root,
        predecessor_snapshot_id=selection.predecessor_snapshot_id,
        active_snapshot_id=selection.active_snapshot_id,
        publication_generation=selection.publication_generation,
        include_predecessor=engine in {"paired", "v1"},
        include_active=engine in {"paired", "v2"},
    )

    def predecessor_probe(
        workload: str,
        payload: _QueryPayload,
    ) -> ProbeTelemetry:
        del workload
        if pinned_pair.predecessor is None:
            raise PerformanceEvidenceError(
                "predecessor probe is unavailable in this worker"
            )
        return _native_probe(pinned_pair.predecessor.reader, payload)

    def successor_probe(workload: str, payload: _QueryPayload) -> ProbeTelemetry:
        del workload
        if pinned_pair.active is None:
            raise PerformanceEvidenceError(
                "successor probe is unavailable in this worker"
            )
        return _native_probe(pinned_pair.active.reader, payload)

    return BenchmarkFactory(
        queries=queries,
        v1_probe=predecessor_probe,
        v2_probe=successor_probe,
        environment={
            "os": platform.system().lower(),
            "os_release": platform.release(),
            "python_version": platform.python_version(),
            "faiss_version": getattr(faiss, "__version__", "unknown"),
            "numpy_version": np.__version__,
            "cache_state": "warm",
            "reranker": "disabled-reader-parity",
            "query_input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "benchmark_pair": "native_predecessor_vs_native_successor",
            "catalog_policy": "shared_checkpointed_catalog_clone_pinned_revisions",
            "baseline_snapshot_id": selection.predecessor_snapshot_id,
            "baseline_snapshot_sha256": predecessor_descriptor["sha256"],
            "baseline_ntotal": predecessor_descriptor["ntotal"],
            "candidate_snapshot_id": selection.active_snapshot_id,
            "candidate_snapshot_sha256": active_descriptor["sha256"],
            "candidate_ntotal": active_descriptor["ntotal"],
            "dimension": active_descriptor["dimension"],
            "metric": active_descriptor["metric"],
            "k": k,
            "write_epoch": selection.write_epoch,
            "v1_fallback_open": selection.v1_fallback_open,
        },
        engine=engine,
    )


@dataclass
class _PinnedSnapshotReader:
    repository: CatalogRepository
    reader: NativeRetrievalReader


@dataclass
class _PinnedSnapshotPair:
    temporary: Path
    predecessor: _PinnedSnapshotReader | None
    active: _PinnedSnapshotReader | None


class _PinnedCatalogRepository(CatalogRepository):
    def __init__(
        self,
        catalog_path: Path,
        data_root: Path,
        revision: SnapshotRevision,
    ) -> None:
        super().__init__(catalog_path, data_root=data_root)
        self._pinned_revision = revision

    def _read_active_revision(
        self,
        connection: sqlite3.Connection,
    ) -> SnapshotRevision:
        del connection
        return self._pinned_revision


def _benchmark_inputs() -> tuple[Path, bytes, Any]:
    root_value = os.environ.get("V2_BENCHMARK_DATA_ROOT")
    input_value = os.environ.get("V2_BENCHMARK_INPUT")
    if not root_value or not input_value:
        raise PerformanceEvidenceError(
            "V2_BENCHMARK_DATA_ROOT and V2_BENCHMARK_INPUT are required"
        )
    data_root = Path(root_value).resolve(strict=True)
    input_path = Path(input_value).resolve(strict=True)
    if not data_root.is_dir() or data_root.is_symlink():
        raise PerformanceEvidenceError("benchmark data root is unsafe")
    if not input_path.is_file() or input_path.is_symlink():
        raise PerformanceEvidenceError("benchmark input file is unsafe")
    input_bytes = input_path.read_bytes()
    try:
        specification = json.loads(input_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PerformanceEvidenceError("benchmark input JSON is invalid") from exc
    return data_root, input_bytes, specification


def _pinned_snapshot_pair(
    source_catalog: Path,
    data_root: Path,
    *,
    predecessor_snapshot_id: str,
    active_snapshot_id: str,
    publication_generation: int,
    include_predecessor: bool,
    include_active: bool,
) -> _PinnedSnapshotPair:
    temporary = Path(tempfile.mkdtemp(prefix="v2-successor-benchmark-")).resolve()
    catalog = temporary / "catalog.sqlite3"
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    predecessor: _PinnedSnapshotReader | None = None
    active: _PinnedSnapshotReader | None = None
    try:
        try:
            source = sqlite3.connect(
                f"file:{source_catalog.resolve().as_posix()}?mode=ro",
                uri=True,
            )
            target = sqlite3.connect(catalog)
            source.backup(target)
            configure_catalog_storage(target, initialize=True, writable=True)
            checkpoint_isolated_catalog(target)
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
        predecessor = (
            _pinned_snapshot_reader(
                catalog,
                data_root,
                predecessor_snapshot_id,
                publication_generation=max(0, publication_generation - 1),
            )
            if include_predecessor
            else None
        )
        active = (
            _pinned_snapshot_reader(
                catalog,
                data_root,
                active_snapshot_id,
                publication_generation=publication_generation,
            )
            if include_active
            else None
        )
    except Exception:
        repositories = tuple(
            holder.repository for holder in (predecessor, active) if holder is not None
        )
        _close_pinned_pair(repositories, temporary)
        raise
    pair = _PinnedSnapshotPair(
        temporary=temporary,
        predecessor=predecessor,
        active=active,
    )
    repositories = tuple(
        holder.repository for holder in (predecessor, active) if holder is not None
    )
    weakref.finalize(pair, _close_pinned_pair, repositories, temporary)
    return pair


def _pinned_snapshot_reader(
    catalog: Path,
    data_root: Path,
    snapshot_id: str,
    *,
    publication_generation: int,
) -> _PinnedSnapshotReader:
    revision = _snapshot_revision(
        catalog,
        data_root,
        snapshot_id,
        publication_generation=publication_generation,
    )
    repository = _PinnedCatalogRepository(catalog, data_root, revision)
    return _PinnedSnapshotReader(
        repository=repository,
        reader=NativeRetrievalReader(repository),
    )


def _close_pinned_pair(
    repositories: tuple[CatalogRepository, ...],
    temporary: Path,
) -> None:
    for repository in repositories:
        try:
            repository.close()
        except Exception:
            # Cleanup is used from both an exception handler and weakref.finalize.
            # One broken close must not mask the original failure or strand peers.
            pass
    shutil.rmtree(temporary, ignore_errors=True)


def _snapshot_revision(
    catalog: Path,
    data_root: Path,
    snapshot_id: str,
    *,
    publication_generation: int,
) -> SnapshotRevision:
    connection = sqlite3.connect(
        f"file:{catalog.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    try:
        configure_catalog_storage(connection)
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT snapshot.build_id, build.profile_id, snapshot.relative_path,
                   snapshot.file_sha256, snapshot.size_bytes,
                   snapshot.dimension, snapshot.metric, snapshot.ntotal
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            WHERE snapshot.snapshot_id = ?
              AND snapshot.state = 'ready'
              AND build.state = 'fully_complete'
            """,
            (snapshot_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise PerformanceEvidenceError("benchmark snapshot is not fully complete")
    relative_path = PurePosixPath(str(row[2]))
    snapshot_path = data_root.joinpath(*relative_path.parts).resolve()
    try:
        snapshot_path.relative_to(data_root)
    except ValueError as exc:
        raise PerformanceEvidenceError("benchmark snapshot escapes the data root") from exc
    return SnapshotRevision(
        catalog_path=catalog.resolve(),
        publication_generation=publication_generation,
        snapshot_id=snapshot_id,
        build_id=str(row[0]),
        profile_id=str(row[1]),
        snapshot_path=snapshot_path,
        descriptor=SnapshotDescriptor(
            sha256=str(row[3]),
            size_bytes=int(row[4]),
            dimension=int(row[5]),
            metric=str(row[6]),
            ntotal=int(row[7]),
        ),
    )


def _native_probe(
    reader: NativeRetrievalReader,
    payload: _QueryPayload,
) -> ProbeTelemetry:
    response = reader.search(payload.vector, payload.k, payload.scope)
    if response.timings is None:
        raise PerformanceEvidenceError("native reader did not emit timings")
    return ProbeTelemetry(
        sql_ns=response.timings.eligibility_ns + response.timings.hydration_ns,
        sql_rows=response.eligible_count + len(response.results),
        strategy=response.strategy.value,
        faiss_ns=response.timings.faiss_ns,
        faiss_calls=response.faiss_calls,
        faiss_candidates=response.candidate_count,
        hydration_batches=response.hydration_batches,
        hydration_rows=response.hydration_rows,
        rerank_ns=0,
        lease_ns=response.timings.lease_ns,
        hydration_cache_hits=response.hydration_cache_hits,
        hydration_cache_misses=response.hydration_cache_misses,
    )


def _active_descriptor(catalog: Path, snapshot_id: str) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{catalog.resolve().as_posix()}?mode=ro", uri=True)
    try:
        try:
            configure_catalog_storage(connection)
        except SchemaError as exc:
            raise PerformanceEvidenceError(
                "benchmark catalog storage mode is invalid"
            ) from exc
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            """
            SELECT file_sha256, dimension, metric, ntotal
            FROM vector_snapshots
            WHERE snapshot_id = ? AND state = 'ready'
            """,
            (snapshot_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise PerformanceEvidenceError("active benchmark descriptor is unavailable")
    return {
        "sha256": str(row[0]),
        "dimension": int(row[1]),
        "metric": str(row[2]),
        "ntotal": int(row[3]),
    }


def _load_queries(
    value: Any,
    *,
    dimension: int,
    v1_ntotal: int,
) -> tuple[dict[str, tuple[FixedBenchmarkQuery, ...]], int]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "kind",
        "k",
        "queries",
        "workloads",
    }:
        raise PerformanceEvidenceError("copied-install benchmark input fields are invalid")
    if value["schema_version"] != 1 or value["kind"] != "v2_retrieval_query_vectors":
        raise PerformanceEvidenceError("copied-install benchmark input kind is invalid")
    k = value["k"]
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise PerformanceEvidenceError("benchmark k must be a positive integer")
    if k > v1_ntotal:
        raise PerformanceEvidenceError("benchmark k exceeds the V1 corpus size")
    raw_queries = value["queries"]
    if not isinstance(raw_queries, list):
        raise PerformanceEvidenceError("benchmark queries must be an array")
    vectors: list[tuple[str, np.ndarray]] = []
    for query in raw_queries:
        if not isinstance(query, dict) or set(query) != {"query_id", "vector"}:
            raise PerformanceEvidenceError("benchmark query fields are invalid")
        vector = np.asarray(query["vector"], dtype=np.float32)
        if vector.shape != (dimension,) or not np.isfinite(vector).all():
            raise PerformanceEvidenceError(
                "benchmark query vector has the wrong shape or non-finite values"
            )
        vectors.append((query["query_id"], np.ascontiguousarray(vector)))

    workloads = value["workloads"]
    if not isinstance(workloads, dict) or set(workloads) != set(REQUIRED_WORKLOADS):
        raise PerformanceEvidenceError("benchmark workload definitions are incomplete")
    result: dict[str, tuple[FixedBenchmarkQuery, ...]] = {}
    for workload in REQUIRED_WORKLOADS:
        definition = workloads[workload]
        if not isinstance(definition, dict) or set(definition) != {"scope"}:
            raise PerformanceEvidenceError(
                f"benchmark workload definition is invalid: {workload}"
            )
        scope = definition.get("scope")
        if scope is not None and not isinstance(scope, dict):
            raise PerformanceEvidenceError("benchmark scope must be an object or null")
        compile_scope_filters(scope)
        if workload == "unfiltered" and scope is not None:
            raise PerformanceEvidenceError("unfiltered workload scope must be null")
        if workload != "unfiltered" and not scope:
            raise PerformanceEvidenceError(
                f"filtered workload requires a non-empty scope: {workload}"
            )
        # Mirror the epoch-zero production dispatcher exactly: an unfiltered
        # request asks FAISS only for k, while any filtered request retrieves N
        # and applies metadata filtering afterward.
        fetch_k = k if scope is None else v1_ntotal
        result[workload] = tuple(
            FixedBenchmarkQuery(
                query_id,
                _QueryPayload(vector, scope, k, fetch_k),
            )
            for query_id, vector in vectors
        )
    return result, k


def _metadata_predicate(scope: dict[str, Any] | None):
    if not scope:
        return None
    compiled = compile_scope_filters(scope)
    if compiled.is_empty:
        return lambda _metadata: False

    def predicate(metadata: dict[str, Any]) -> bool:
        values = _flatten_scope(scope)
        file_name = str(metadata.get("file_name", ""))
        canonical = str(
            metadata.get("canonical_relative_path")
            or PurePosixPath("downloaded") / file_name
        )
        scalar_fields = {
            "target_name": "target_name",
            "report_type": "report_type",
            "report_date": "report_date",
            "broker": "broker",
            "file_name": "file_name",
            "prior_file_name": "file_name",
            "canonical_relative_path": "canonical_relative_path",
            "prior_path": "canonical_relative_path",
        }
        metadata_values = {**metadata, "canonical_relative_path": canonical}
        for scope_field, metadata_field in scalar_fields.items():
            if scope_field in values and str(metadata_values.get(metadata_field, "")) != str(
                values[scope_field]
            ):
                return False
        sequence_fields = {
            "target_names": "target_name",
            "report_types": "report_type",
            "brokers": "broker",
            "file_names": "file_name",
            "prior_file_names": "file_name",
            "canonical_relative_paths": "canonical_relative_path",
            "prior_paths": "canonical_relative_path",
        }
        for scope_field, metadata_field in sequence_fields.items():
            if scope_field in values and str(metadata_values.get(metadata_field, "")) not in {
                str(item) for item in values[scope_field]
            }:
                return False
        report_date = str(metadata.get("report_date", ""))
        start = values.get("report_date_start", values.get("date_start"))
        end = values.get("report_date_end", values.get("date_end"))
        return not (
            (start is not None and report_date < str(start))
            or (end is not None and report_date > str(end))
        )

    return predicate


def _flatten_scope(scope: dict[str, Any]) -> dict[str, Any]:
    values = dict(scope)
    prior = values.pop("prior_scope", None)
    if isinstance(prior, dict):
        for source, target in (
            ("file_name", "prior_file_name"),
            ("file_names", "prior_file_names"),
            ("canonical_relative_path", "prior_path"),
            ("canonical_relative_paths", "prior_paths"),
            ("path", "prior_path"),
            ("paths", "prior_paths"),
        ):
            if source in prior:
                values[target] = prior[source]
    for ignored in ("scope_source", "reason", "matched_section_id", "empty"):
        values.pop(ignored, None)
    aliases = {
        "target": "target_name",
        "targets": "target_names",
        "date": "report_date",
        "path": "canonical_relative_path",
        "paths": "canonical_relative_paths",
        "file_path": "canonical_relative_path",
        "file_paths": "canonical_relative_paths",
    }
    for source, target in aliases.items():
        if source in values:
            values[target] = values.pop(source)
    company = values.pop("company", values.pop("company_name", None))
    if company is True:
        values["report_type"] = "company"
    elif company not in (None, False):
        values["report_type"] = "company"
        values["target_name"] = company
    return values


__all__ = ["create_factory", "create_successor_factory"]
