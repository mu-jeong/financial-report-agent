"""Narrow read-only boundary between the MCP integration and Native V2.

Only this module imports application code.  Metadata reads use the upstream
``active_reports`` view in a read-only SQLite transaction, while vector search
uses the public repository and reader APIs so sparse delta overlays retain the
same request-scoped semantics as the application.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import numpy as np
import requests

from integrations.finance_mcp.contracts import ToolFailure


class _MissingUpstreamError(Exception):
    pass


_UPSTREAM_IMPORT_ERROR: BaseException | None = None
try:
    from src.configs.config import (
        EMBEDDING_MODEL,
        OPENROUTER_API_KEY,
        OPENROUTER_APP_TITLE,
        OPENROUTER_APP_URL,
        OPENROUTER_DATA_COLLECTION,
    )
    from src.llms.embeddings import OpenRouterEmbeddings
    from src.retrieval.bootstrap import RetrievalBootstrapError, inspect_runtime
    from src.retrieval.reader import NativeRetrievalReader
    from src.retrieval.repository import (
        CatalogRepository,
        RepositoryError,
        ScopeValidationError,
        SnapshotUnavailableError,
        SnapshotValidationError,
        compile_scope_filters,
    )
except (ImportError, TypeError, AttributeError) as exc:
    _UPSTREAM_IMPORT_ERROR = exc
    EMBEDDING_MODEL = ""
    OPENROUTER_API_KEY = ""
    OPENROUTER_APP_TITLE = ""
    OPENROUTER_APP_URL = ""
    OPENROUTER_DATA_COLLECTION = ""
    OpenRouterEmbeddings = None
    inspect_runtime = None
    NativeRetrievalReader = None
    CatalogRepository = None
    compile_scope_filters = None
    RetrievalBootstrapError = _MissingUpstreamError
    RepositoryError = _MissingUpstreamError
    ScopeValidationError = _MissingUpstreamError
    SnapshotUnavailableError = _MissingUpstreamError
    SnapshotValidationError = _MissingUpstreamError


_REPORT_COLUMNS = """
    report_uid, canonical_relative_path, report_type, report_date,
    target_name, title, broker
"""
_GROUP_COLUMNS = frozenset({"target_name", "report_type", "broker", "report_date"})
_EXCERPT_CHARS = 4_000


class UpstreamAdapter:
    """Expose bounded Native V2 reads without changing the application."""

    def __init__(self, data_root: Path, *, embedding_timeout: float = 30.0) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.embedding_timeout = float(embedding_timeout)
        self._repository: CatalogRepository | None = None
        self._reader: NativeRetrievalReader | None = None
        self._embeddings: OpenRouterEmbeddings | None = None

    def get_status(self) -> dict[str, Any]:
        self._ensure_upstream()
        try:
            selection = inspect_runtime(
                self.data_root,
                validate_snapshot=False,
                catalog_validation="read",
            )
        except (
            RetrievalBootstrapError,
            OSError,
            sqlite3.Error,
            TypeError,
            AttributeError,
        ) as exc:
            raise self._incompatible(exc) from exc

        if not selection.is_native or selection.is_empty:
            state = "not_ready" if not selection.is_empty else "empty"
            return self._result(
                {
                    "state": state,
                    "report_count": 0,
                    "date_range": {"start": None, "end": None},
                    "capabilities": self._capabilities(available=selection.is_empty, search=False),
                },
                revision=None,
            )

        with self._metadata_transaction() as (connection, revision, profile):
            row = connection.execute(
                """
                SELECT count(*) AS report_count,
                       min(report_date) AS first_date,
                       max(report_date) AS last_date
                FROM active_reports
                """
            ).fetchone()
            compatible = profile["model"] == EMBEDDING_MODEL
            search_available = compatible and bool(OPENROUTER_API_KEY)
            warnings = [] if compatible else ["active embedding profile differs from configured model"]
            return self._result(
                {
                    "state": "ready",
                    "report_count": int(row["report_count"]),
                    "date_range": {
                        "start": row["first_date"],
                        "end": row["last_date"],
                    },
                    "embedding_profile": {
                        "profile_id": profile["profile_id"],
                        "model": profile["model"],
                        "dimension": int(profile["dimension"]),
                        "metric": profile["metric"],
                        "configured_model_compatible": compatible,
                    },
                    "snapshot_validated": False,
                    "capabilities": self._capabilities(available=True, search=search_available),
                },
                revision=revision,
                warnings=warnings,
            )

    def list_reports(
        self,
        filters: dict[str, Any],
        limit: int,
        offset: int,
        expected_revision: str | None,
    ) -> dict[str, Any]:
        predicate, parameters, provably_empty = self._compile_metadata_filters(filters)
        with self._metadata_transaction(expected_revision) as (connection, revision, _):
            if connection is None or provably_empty:
                return self._result(
                    {"reports": [], "total_count": 0},
                    revision=revision,
                )
            total = int(
                connection.execute(
                    f"SELECT count(*) FROM active_reports AS report WHERE {predicate}",
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT {_REPORT_COLUMNS}
                FROM active_reports AS report
                WHERE {predicate}
                ORDER BY report_date DESC, report_uid ASC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit + 1, offset),
            ).fetchall()
            has_more = len(rows) > limit
            reports = [self._report_dict(row) for row in rows[:limit]]
            return self._result(
                {"reports": reports, "total_count": total},
                revision=revision,
                next_offset=offset + limit if has_more else None,
            )

    def get_report_stats(
        self,
        filters: dict[str, Any],
        group_by: str | None,
        limit: int,
        offset: int,
        expected_revision: str | None,
    ) -> dict[str, Any]:
        if group_by is not None and group_by not in _GROUP_COLUMNS:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "group_by must be target_name, report_type, broker, or report_date",
                retryable=False,
            )
        predicate, parameters, provably_empty = self._compile_metadata_filters(filters)
        with self._metadata_transaction(expected_revision) as (connection, revision, _):
            if connection is None or provably_empty:
                return self._result(
                    {"total_count": 0, "group_by": group_by, "groups": []},
                    revision=revision,
                )
            total = int(
                connection.execute(
                    f"SELECT count(*) FROM active_reports AS report WHERE {predicate}",
                    parameters,
                ).fetchone()[0]
            )
            groups: list[dict[str, Any]] = []
            next_offset = None
            if group_by is not None:
                rows = connection.execute(
                    f"""
                    SELECT {group_by} AS group_value, count(*) AS report_count
                    FROM active_reports AS report
                    WHERE {predicate}
                    GROUP BY {group_by}
                    ORDER BY report_count DESC, group_value ASC
                    LIMIT ? OFFSET ?
                    """,
                    (*parameters, limit + 1, offset),
                ).fetchall()
                if len(rows) > limit:
                    next_offset = offset + limit
                groups = [
                    {"value": row["group_value"], "report_count": int(row["report_count"])}
                    for row in rows[:limit]
                ]
            return self._result(
                {"total_count": total, "group_by": group_by, "groups": groups},
                revision=revision,
                next_offset=next_offset,
            )

    def search_reports(
        self,
        query: str,
        filters: dict[str, Any],
        top_k: int,
    ) -> dict[str, Any]:
        try:
            compiled = compile_scope_filters(filters)
        except ScopeValidationError as exc:
            raise ToolFailure("INVALID_ARGUMENT", str(exc), retryable=False) from exc
        selection = self._require_ready(allow_empty=True, validate_snapshot=False)
        if selection.is_empty:
            return self._empty_search(revision=None)
        with self._metadata_transaction() as (connection, captured_revision, profile):
            assert connection is not None
            assert profile is not None
            if compiled.is_empty:
                return self._empty_search(revision=captured_revision)
            eligible = connection.execute(
                f"SELECT 1 FROM active_reports AS report WHERE {compiled.predicate_sql} LIMIT 1",
                compiled.parameters,
            ).fetchone()
            if eligible is None:
                return self._empty_search(revision=captured_revision)
        if profile["model"] != EMBEDDING_MODEL:
            raise ToolFailure(
                "UPSTREAM_INCOMPATIBLE",
                "configured embedding model does not match the active index profile",
                retryable=False,
            )
        try:
            vector = np.asarray(self._embedding_model().embed_query(query), dtype=np.float32)
        except requests.Timeout as exc:
            raise ToolFailure("TIMEOUT", "embedding provider timed out", retryable=True) from exc
        except (
            requests.RequestException,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
        ) as exc:
            raise ToolFailure(
                "PROVIDER_UNAVAILABLE",
                "embedding provider request failed",
                retryable=True,
            ) from exc

        if (
            vector.ndim != 1
            or vector.size != int(profile["dimension"])
            or not bool(np.isfinite(vector).all())
        ):
            raise ToolFailure(
                "PROVIDER_UNAVAILABLE",
                "embedding provider returned an incompatible vector",
                retryable=True,
            )
        with self._metadata_transaction() as (_connection, current_revision, current_profile):
            assert current_profile is not None
        if (
            current_revision != captured_revision
            or current_profile["profile_id"] != profile["profile_id"]
        ):
            raise ToolFailure(
                "STALE_REVISION",
                "the active report revision changed while embedding the query",
                retryable=True,
            )
        try:
            response = self._native_reader(selection.paths.catalog).search(
                vector,
                top_k,
                scope=filters,
            )
        except (
            SnapshotUnavailableError,
            SnapshotValidationError,
            RepositoryError,
            OSError,
            TypeError,
            AttributeError,
            ValueError,
            RuntimeError,
        ) as exc:
            with self._metadata_transaction() as (
                _connection,
                failed_revision,
                failed_profile,
            ):
                pass
            if (
                failed_revision != captured_revision
                or failed_profile is None
                or failed_profile["profile_id"] != profile["profile_id"]
            ):
                raise ToolFailure(
                    "STALE_REVISION",
                    "the active report revision changed while searching",
                    retryable=True,
                ) from exc
            raise self._incompatible(exc) from exc

        revision = self._revision_from_native(response.revision)
        if (
            response.revision.profile_id != profile["profile_id"]
            or revision != captured_revision
        ):
            raise ToolFailure(
                "STALE_REVISION",
                "the active report revision changed while embedding the query",
                retryable=True,
            )
        score_semantics = {
            "metric": profile["metric"],
            "better": "lower" if profile["metric"] == "l2" else "higher",
        }
        results = [
            {
                "rank": hit.rank,
                "score": hit.score,
                "score_semantics": score_semantics,
                "report_uid": hit.report_uid,
                "chunk_uid": hit.chunk_uid,
                "parent_uid": hit.parent_uid,
                "title": hit.title,
                "report_date": hit.report_date,
                "target_name": hit.target_name,
                "report_type": hit.report_type,
                "broker": hit.broker,
                "canonical_relative_path": hit.canonical_relative_path,
                "excerpt": hit.parent_slice[:_EXCERPT_CHARS],
                "excerpt_truncated": len(hit.parent_slice) > _EXCERPT_CHARS,
            }
            for hit in response.results
        ]
        return self._result(
            {
                "results": results,
                "strategy": response.strategy.value,
                "eligible_report_chunks": response.eligible_count,
                "reranked": False,
                "score_semantics": score_semantics,
            },
            revision=revision,
        )

    def read_report(
        self,
        report_uid: str,
        expected_revision: str | None,
        offset: int,
        max_chars: int,
    ) -> dict[str, Any]:
        with self._metadata_transaction(expected_revision) as (connection, revision, profile):
            if connection is None or profile is None:
                raise ToolFailure("REPORT_NOT_FOUND", "report is not active", retryable=False)
            report = connection.execute(
                f"SELECT {_REPORT_COLUMNS} FROM active_reports WHERE report_uid = ?",
                (report_uid,),
            ).fetchone()
            if report is None:
                raise ToolFailure("REPORT_NOT_FOUND", "report is not active", retryable=False)
            parent_layout = connection.execute(
                """
                WITH active_parent AS (
                    SELECT DISTINCT parent.parent_uid, parent.parent_order,
                           length(parent.content) AS content_length
                    FROM retrieval_parents AS parent
                    JOIN retrieval_chunks AS chunk
                      ON chunk.parent_uid = parent.parent_uid
                     AND chunk.profile_id = parent.profile_id
                    JOIN active_vector_membership AS membership
                      ON membership.chunk_uid = chunk.chunk_uid
                    JOIN active_reports AS report ON report.report_id = parent.report_id
                    WHERE report.report_uid = ? AND parent.profile_id = ?
                )
                SELECT parent_uid, parent_order, content_length,
                       COALESCE(sum(content_length + 2) OVER (
                           ORDER BY parent_order, parent_uid
                           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                       ), 0) AS content_start
                FROM active_parent
                ORDER BY parent_order, parent_uid
                """,
                (report_uid, profile["profile_id"]),
            ).fetchall()
            if not parent_layout:
                raise ToolFailure(
                    "UPSTREAM_INCOMPATIBLE",
                    "active report has no text for the active profile",
                    retryable=False,
                )
            total_chars = sum(int(row["content_length"]) for row in parent_layout)
            total_chars += 2 * (len(parent_layout) - 1)
            window_end = min(total_chars, offset + max_chars)
            fragment_rows = connection.execute(
                """
                WITH active_parent AS (
                    SELECT DISTINCT parent.parent_uid, parent.parent_order,
                           length(parent.content) AS content_length
                    FROM retrieval_parents AS parent
                    JOIN retrieval_chunks AS chunk
                      ON chunk.parent_uid = parent.parent_uid
                     AND chunk.profile_id = parent.profile_id
                    JOIN active_vector_membership AS membership
                      ON membership.chunk_uid = chunk.chunk_uid
                    JOIN active_reports AS report ON report.report_id = parent.report_id
                    WHERE report.report_uid = ? AND parent.profile_id = ?
                ), positioned AS (
                    SELECT parent_uid, parent_order, content_length,
                           COALESCE(sum(content_length + 2) OVER (
                               ORDER BY parent_order, parent_uid
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                           ), 0) AS content_start
                    FROM active_parent
                )
                SELECT positioned.parent_uid,
                       max(?, positioned.content_start) AS fragment_start,
                       substr(
                           parent.content,
                           max(?, positioned.content_start)
                               - positioned.content_start + 1,
                           min(?, positioned.content_start + positioned.content_length)
                               - max(?, positioned.content_start)
                       ) AS fragment
                FROM positioned
                JOIN retrieval_parents AS parent
                  ON parent.parent_uid = positioned.parent_uid
                WHERE positioned.content_start < ?
                  AND positioned.content_start + positioned.content_length > ?
                ORDER BY positioned.parent_order, positioned.parent_uid
                """,
                (
                    report_uid,
                    profile["profile_id"],
                    offset,
                    offset,
                    window_end,
                    offset,
                    window_end,
                    offset,
                ),
            ).fetchall()
            fragments = {
                row["parent_uid"]: (int(row["fragment_start"]), str(row["fragment"]))
                for row in fragment_rows
            }
            pieces: list[tuple[int, str]] = []
            for index, row in enumerate(parent_layout):
                content_start = int(row["content_start"])
                if index:
                    separator_start = content_start - 2
                    overlap_start = max(offset, separator_start)
                    overlap_end = min(window_end, content_start)
                    if overlap_start < overlap_end:
                        start = overlap_start - separator_start
                        pieces.append((overlap_start, "\n\n"[start : start + overlap_end - overlap_start]))
                fragment = fragments.get(row["parent_uid"])
                if fragment is not None:
                    pieces.append(fragment)
            content = "".join(piece for _start, piece in sorted(pieces))
            next_offset = window_end if window_end < total_chars else None
            return self._result(
                {
                    "report": self._report_dict(report),
                    "content": content,
                    "offset": offset,
                    "total_chars": total_chars,
                    "content_kind": "indexed_extracted_text",
                },
                revision=revision,
                next_offset=next_offset,
            )

    def close(self) -> None:
        if self._repository is not None:
            self._repository.close()
        self._repository = None
        self._reader = None
        self._embeddings = None

    def _require_ready(
        self,
        *,
        allow_empty: bool = False,
        validate_snapshot: bool = False,
    ):
        self._ensure_upstream()
        try:
            selection = inspect_runtime(
                self.data_root,
                validate_snapshot=validate_snapshot,
                catalog_validation="read",
            )
        except (
            RetrievalBootstrapError,
            OSError,
            sqlite3.Error,
            TypeError,
            AttributeError,
        ) as exc:
            raise self._incompatible(exc) from exc
        if not selection.is_native or (selection.is_empty and not allow_empty):
            raise ToolFailure("NOT_READY", "report index is not ready", retryable=True)
        return selection

    def _native_reader(self, catalog: Path) -> NativeRetrievalReader:
        self._ensure_upstream()
        if self._reader is None:
            self._repository = CatalogRepository(catalog, data_root=self.data_root)
            self._reader = NativeRetrievalReader(self._repository)
        return self._reader

    def _embedding_model(self) -> OpenRouterEmbeddings:
        self._ensure_upstream()
        if self._embeddings is None:
            try:
                self._embeddings = OpenRouterEmbeddings(
                    model=EMBEDDING_MODEL,
                    api_key=OPENROUTER_API_KEY or "",
                    timeout=self.embedding_timeout,
                    app_url=OPENROUTER_APP_URL,
                    app_title=OPENROUTER_APP_TITLE,
                    data_collection=OPENROUTER_DATA_COLLECTION,
                    max_retries=0,
                )
            except ValueError as exc:
                raise ToolFailure(
                    "PROVIDER_UNAVAILABLE",
                    "embedding provider is not configured",
                    retryable=False,
                ) from exc
        return self._embeddings

    @contextmanager
    def _metadata_transaction(
        self, expected_revision: str | None = None
    ) -> Iterator[tuple[sqlite3.Connection | None, str | None, sqlite3.Row | None]]:
        selection = self._require_ready(allow_empty=True, validate_snapshot=False)
        if selection.is_empty:
            if expected_revision is not None:
                raise ToolFailure(
                    "STALE_REVISION",
                    "the active report revision has changed",
                    retryable=True,
                )
            yield None, None, None
            return
        connection = self._open_read_only(selection.paths.catalog)
        try:
            connection.execute("BEGIN")
            revision, profile = self._read_revision(connection)
            if expected_revision is not None and expected_revision != revision:
                raise ToolFailure(
                    "STALE_REVISION",
                    "the active report revision has changed",
                    retryable=True,
                )
            yield connection, revision, profile
        except ToolFailure:
            raise
        except (sqlite3.Error, TypeError, AttributeError, IndexError, ValueError) as exc:
            raise self._incompatible(exc) from exc
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    @staticmethod
    def _open_read_only(catalog: Path) -> sqlite3.Connection:
        uri = f"file:{quote(str(catalog.resolve()), safe='/:')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _read_revision(connection: sqlite3.Connection) -> tuple[str, sqlite3.Row]:
        row = connection.execute(
            """
            SELECT runtime.schema_version, runtime.publication_generation,
                   runtime.active_snapshot_id, runtime.active_build_id,
                   build.profile_id, profile.profile_hash, profile.model,
                   profile.dimension, profile.metric,
                   COALESCE((
                       SELECT max(segment.sequence)
                       FROM retrieval_delta_segments AS segment
                       WHERE segment.state = 'ready'
                         AND segment.base_snapshot_id = runtime.active_snapshot_id
                         AND segment.base_publication_generation = runtime.publication_generation
                   ), 0) AS delta_generation,
                   (SELECT count(*)
                    FROM retrieval_delta_segments AS segment
                    WHERE segment.state = 'ready'
                      AND segment.base_snapshot_id = runtime.active_snapshot_id
                      AND segment.base_publication_generation = runtime.publication_generation
                   ) AS delta_segment_count
            FROM retrieval_runtime AS runtime
            JOIN retrieval_builds AS build ON build.build_id = runtime.active_build_id
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            WHERE runtime.runtime_id = 1
              AND build.state = 'fully_complete'
            """
        ).fetchone()
        if row is None:
            raise ToolFailure("NOT_READY", "report index is not ready", retryable=True)
        payload = {
            "publication": int(row["publication_generation"]),
            "snapshot": row["active_snapshot_id"],
            "build": row["active_build_id"],
            "profile": row["profile_id"],
            "delta": int(row["delta_generation"]),
            "delta_segments": int(row["delta_segment_count"]),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"native-v2:{digest}", row

    @staticmethod
    def _compile_metadata_filters(
        filters: dict[str, Any],
    ) -> tuple[str, tuple[object, ...], bool]:
        UpstreamAdapter._ensure_upstream()
        try:
            compiled = compile_scope_filters(filters)
        except ScopeValidationError as exc:
            raise ToolFailure("INVALID_ARGUMENT", str(exc), retryable=False) from exc
        return compiled.predicate_sql, compiled.parameters, compiled.is_empty

    @staticmethod
    def _report_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "report_uid": row["report_uid"],
            "canonical_relative_path": row["canonical_relative_path"],
            "report_type": row["report_type"],
            "report_date": row["report_date"],
            "target_name": row["target_name"],
            "title": row["title"],
            "broker": row["broker"],
        }

    @staticmethod
    def _revision_from_native(revision: Any) -> str:
        payload = {
            "publication": revision.publication_generation,
            "snapshot": revision.snapshot_id,
            "build": revision.build_id,
            "profile": revision.profile_id,
            "delta": revision.delta_generation,
            "delta_segments": revision.delta_segment_count,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"native-v2:{digest}"

    @staticmethod
    def _capabilities(*, available: bool, search: bool) -> dict[str, bool]:
        return {
            "list_reports": available,
            "get_report_stats": available,
            "search_reports": available and search,
            "read_report": available,
        }

    @classmethod
    def _empty_search(cls, *, revision: str | None) -> dict[str, Any]:
        return cls._result(
            {
                "results": [],
                "strategy": "empty",
                "eligible_report_chunks": 0,
                "reranked": False,
                "score_semantics": None,
            },
            revision=revision,
        )

    @staticmethod
    def _result(
        data: dict[str, Any],
        *,
        revision: str | None,
        next_offset: int | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "data": data,
            "revision": revision,
            "next_offset": next_offset,
            "warnings": warnings or [],
        }

    @staticmethod
    def _incompatible(_exc: BaseException) -> ToolFailure:
        return ToolFailure(
            "UPSTREAM_INCOMPATIBLE",
            "native report runtime is unavailable or incompatible",
            retryable=False,
        )

    @staticmethod
    def _ensure_upstream() -> None:
        if _UPSTREAM_IMPORT_ERROR is not None:
            raise UpstreamAdapter._incompatible(_UPSTREAM_IMPORT_ERROR)


__all__ = ["UpstreamAdapter"]
