"""Minimal immutable registry for release-scoped issue reproduction.

The registry deliberately stores only explicit workflow state. Asset availability and
issue progress are derived from immutable records and local bytes by callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from src.core.artifact_io import strict_json_loads


SCHEMA_VERSION = 3
ISSUE_STATUSES = frozenset(
    {"OPEN", "IN_PROGRESS", "RESOLVED", "NOT_ISSUE", "CLOSED"}
)
ISSUE_TERMINAL_STATUSES = frozenset({"RESOLVED", "NOT_ISSUE", "CLOSED"})
ISSUE_STATUS_TRANSITIONS = {
    "OPEN": frozenset({"IN_PROGRESS", "RESOLVED", "NOT_ISSUE"}),
    "IN_PROGRESS": frozenset({"OPEN", "RESOLVED", "NOT_ISSUE"}),
    "RESOLVED": frozenset({"OPEN", "NOT_ISSUE"}),
    "NOT_ISSUE": frozenset({"OPEN", "RESOLVED"}),
    "CLOSED": frozenset({"OPEN", "RESOLVED", "NOT_ISSUE"}),
}
_ISSUE_EVENT_BY_TARGET = {
    "OPEN": "REOPENED",
    "IN_PROGRESS": "IN_PROGRESS",
    "RESOLVED": "RESOLVED",
    "NOT_ISSUE": "NOT_ISSUE",
}
TERMINAL_RUN_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
)
VALID_RUN_SIDES = frozenset({"BASELINE", "CANDIDATE"})
VALID_COMPARISON_VERDICTS = frozenset(
    {"IMPROVED", "NOT_IMPROVED", "REGRESSED", "INCONCLUSIVE"}
)
VALID_CHECK_TYPES = frozenset(
    {
        "ANSWER_CONTAINS",
        "ANSWER_NOT_CONTAINS",
        "EVIDENCE_CONTAINS",
        "CITATION_PRESENT",
        "ROUTE_EQUALS",
        "MANUAL",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FULL_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_EVIDENCE_ROLES = frozenset({"OBSERVED_RESULT", "CONTEXT_USED", "CITED"})


class MonitoringRegistryError(RuntimeError):
    """Base error for registry failures."""


class MonitoringContractError(MonitoringRegistryError):
    """Raised when an operation violates the v8 monitoring contract."""


class RevisionConflictError(MonitoringRegistryError):
    """Raised when an optimistic Issue revision is stale."""


class ImmutableRecordError(MonitoringRegistryError):
    """Raised when immutable history is changed or fails integrity validation."""


class MonitoringRecordNotFound(MonitoringRegistryError):
    """Raised when a requested registry object does not exist."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_object(value: Mapping[str, Any] | None, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MonitoringContractError(f"{label} must be a JSON object")
    return dict(value)


def _nonempty_text(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MonitoringContractError(f"{label} is required")
    return text


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS monitoring_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    source_receipt_id TEXT NOT NULL UNIQUE,
    reported_release_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'NOT_ISSUE', 'CLOSED')
    ),
    summary_json TEXT NOT NULL,
    current_case_contract_id TEXT,
    current_comparison_id TEXT,
    record_revision INTEGER NOT NULL CHECK (record_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_events (
    event_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'CREATED', 'IN_PROGRESS', 'RESOLVED', 'NOT_ISSUE',
            'CLOSED', 'REOPENED', 'RAW_VIEWED',
            'CASE_READY', 'COMPARISON_CREATED'
        )
    ),
    actor_user_id TEXT,
    reason TEXT,
    details_json TEXT NOT NULL,
    before_revision INTEGER,
    after_revision INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fixture_revisions (
    fixture_revision_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    predecessor_fixture_revision_id TEXT REFERENCES fixture_revisions(fixture_revision_id),
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('DRAFT', 'READY')),
    body_json TEXT NOT NULL,
    fixture_digest TEXT,
    created_at TEXT NOT NULL,
    ready_at TEXT
);

CREATE TABLE IF NOT EXISTS fixed_snapshot_revisions (
    fixed_snapshot_revision_id TEXT PRIMARY KEY,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status = 'READY'),
    bundle_relpath TEXT NOT NULL UNIQUE,
    bundle_digest TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    reader_contract_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reproduction_case_revisions (
    case_revision_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    predecessor_case_revision_id TEXT REFERENCES reproduction_case_revisions(case_revision_id),
    fixture_revision_id TEXT NOT NULL REFERENCES fixture_revisions(fixture_revision_id),
    fixed_snapshot_revision_id TEXT NOT NULL REFERENCES fixed_snapshot_revisions(fixed_snapshot_revision_id),
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('DRAFT', 'READY')),
    fixed_clock TEXT,
    evaluator_json TEXT NOT NULL,
    reconstruction_lineage_json TEXT NOT NULL,
    lineage_proof_digest TEXT,
    evidence_qualifier TEXT,
    case_contract_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    ready_at TEXT
);

CREATE TABLE IF NOT EXISTS release_manifests (
    release_manifest_id TEXT PRIMARY KEY,
    release_tag TEXT NOT NULL UNIQUE,
    app_version TEXT NOT NULL,
    manifest_version INTEGER NOT NULL,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status = 'REGISTERED'),
    runtime_bundle_digest TEXT NOT NULL,
    bundle_relpath TEXT NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    case_contract_id TEXT NOT NULL REFERENCES reproduction_case_revisions(case_contract_id),
    release_manifest_id TEXT NOT NULL REFERENCES release_manifests(release_manifest_id),
    side TEXT NOT NULL CHECK (side IN ('BASELINE', 'CANDIDATE')),
    runtime_profile_json TEXT NOT NULL,
    execution_status TEXT NOT NULL CHECK (
        execution_status IN (
            'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'
        )
    ),
    validity TEXT CHECK (validity IN ('VALID', 'INVALID')),
    artifact_relpath TEXT,
    artifact_digest TEXT,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS comparisons (
    comparison_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    case_contract_id TEXT NOT NULL REFERENCES reproduction_case_revisions(case_contract_id),
    baseline_run_ids_json TEXT NOT NULL,
    candidate_run_ids_json TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (
        verdict IN ('IMPROVED', 'NOT_IMPROVED', 'REGRESSED', 'INCONCLUSIVE')
    ),
    note TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    supersedes_comparison_id TEXT REFERENCES comparisons(comparison_id),
    record_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_status_created
    ON issues(status, created_at);
CREATE INDEX IF NOT EXISTS idx_issue_events_issue_created
    ON issue_events(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_fixture_issue_created
    ON fixture_revisions(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_case_issue_created
    ON reproduction_case_revisions(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_issue_case_created
    ON runs(issue_id, case_contract_id, queued_at);
CREATE INDEX IF NOT EXISTS idx_comparisons_issue_case_created
    ON comparisons(issue_id, case_contract_id, created_at);

CREATE TRIGGER IF NOT EXISTS issue_events_no_update
BEFORE UPDATE ON issue_events
BEGIN
    SELECT RAISE(ABORT, 'issue events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS issue_events_no_delete
BEFORE DELETE ON issue_events
BEGIN
    SELECT RAISE(ABORT, 'issue events are immutable');
END;

CREATE TRIGGER IF NOT EXISTS fixture_ready_no_update
BEFORE UPDATE ON fixture_revisions
WHEN OLD.lifecycle_status = 'READY'
BEGIN
    SELECT RAISE(ABORT, 'READY fixture revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS fixture_ready_no_delete
BEFORE DELETE ON fixture_revisions
WHEN OLD.lifecycle_status = 'READY'
BEGIN
    SELECT RAISE(ABORT, 'READY fixture revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS snapshot_no_update
BEFORE UPDATE ON fixed_snapshot_revisions
BEGIN
    SELECT RAISE(ABORT, 'READY fixed snapshots are immutable');
END;

CREATE TRIGGER IF NOT EXISTS snapshot_no_delete
BEFORE DELETE ON fixed_snapshot_revisions
BEGIN
    SELECT RAISE(ABORT, 'READY fixed snapshots cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS case_ready_no_update
BEFORE UPDATE ON reproduction_case_revisions
WHEN OLD.lifecycle_status = 'READY'
BEGIN
    SELECT RAISE(ABORT, 'READY case revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS case_ready_no_delete
BEFORE DELETE ON reproduction_case_revisions
WHEN OLD.lifecycle_status = 'READY'
BEGIN
    SELECT RAISE(ABORT, 'READY case revisions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS release_no_update
BEFORE UPDATE ON release_manifests
BEGIN
    SELECT RAISE(ABORT, 'registered releases are immutable');
END;

CREATE TRIGGER IF NOT EXISTS release_no_delete
BEFORE DELETE ON release_manifests
BEGIN
    SELECT RAISE(ABORT, 'registered releases cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS run_input_no_update
BEFORE UPDATE OF issue_id, case_contract_id, release_manifest_id, side,
                 runtime_profile_json ON runs
BEGIN
    SELECT RAISE(ABORT, 'run inputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS run_status_transition
BEFORE UPDATE OF execution_status ON runs
WHEN NOT (
    (OLD.execution_status = 'QUEUED' AND NEW.execution_status = 'RUNNING')
    OR
    (OLD.execution_status = 'RUNNING' AND NEW.execution_status IN (
        'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'
    ))
)
BEGIN
    SELECT RAISE(ABORT, 'illegal run status transition');
END;

CREATE TRIGGER IF NOT EXISTS run_result_only_at_terminal
BEFORE UPDATE OF validity, artifact_relpath, artifact_digest, completed_at ON runs
WHEN NOT (
    OLD.execution_status = 'RUNNING'
    AND NEW.execution_status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED')
)
BEGIN
    SELECT RAISE(ABORT, 'run result can only be written at terminal transition');
END;

CREATE TRIGGER IF NOT EXISTS run_terminal_no_update
BEFORE UPDATE ON runs
WHEN OLD.execution_status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED')
BEGIN
    SELECT RAISE(ABORT, 'terminal runs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS run_terminal_no_delete
BEFORE DELETE ON runs
WHEN OLD.execution_status IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED')
BEGIN
    SELECT RAISE(ABORT, 'terminal runs cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS comparison_no_update
BEFORE UPDATE ON comparisons
BEGIN
    SELECT RAISE(ABORT, 'comparisons are immutable');
END;

CREATE TRIGGER IF NOT EXISTS comparison_no_delete
BEFORE DELETE ON comparisons
BEGIN
    SELECT RAISE(ABORT, 'comparisons are immutable');
END;
"""


_MIGRATE_SCHEMA_V1_TO_V2_SQL = """
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE issues_v2 (
    issue_id TEXT PRIMARY KEY,
    source_receipt_id TEXT NOT NULL UNIQUE,
    reported_release_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'NOT_ISSUE', 'CLOSED')
    ),
    summary_json TEXT NOT NULL,
    current_case_contract_id TEXT,
    current_comparison_id TEXT,
    record_revision INTEGER NOT NULL CHECK (record_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO issues_v2(
    issue_id, source_receipt_id, reported_release_id, status, summary_json,
    current_case_contract_id, current_comparison_id, record_revision,
    created_at, updated_at
)
SELECT
    issue_id, source_receipt_id, reported_release_id, status, summary_json,
    current_case_contract_id, current_comparison_id, record_revision,
    created_at, updated_at
FROM issues;

CREATE TABLE issue_events_v2 (
    event_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues_v2(issue_id),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'CREATED', 'IN_PROGRESS', 'RESOLVED', 'NOT_ISSUE',
            'CLOSED', 'REOPENED', 'RAW_VIEWED',
            'CASE_READY', 'COMPARISON_CREATED'
        )
    ),
    actor_user_id TEXT,
    reason TEXT,
    details_json TEXT NOT NULL,
    before_revision INTEGER,
    after_revision INTEGER,
    created_at TEXT NOT NULL
);

INSERT INTO issue_events_v2(
    event_id, issue_id, event_type, actor_user_id, reason, details_json,
    before_revision, after_revision, created_at
)
SELECT
    event_id, issue_id, event_type, actor_user_id, reason, details_json,
    before_revision, after_revision, created_at
FROM issue_events;

DROP TABLE issue_events;
DROP TABLE issues;
ALTER TABLE issues_v2 RENAME TO issues;
ALTER TABLE issue_events_v2 RENAME TO issue_events;

CREATE INDEX idx_issues_status_created ON issues(status, created_at);
CREATE INDEX idx_issue_events_issue_created
    ON issue_events(issue_id, created_at);

CREATE TRIGGER issue_events_no_update
BEFORE UPDATE ON issue_events
BEGIN
    SELECT RAISE(ABORT, 'issue events are immutable');
END;

CREATE TRIGGER issue_events_no_delete
BEFORE DELETE ON issue_events
BEGIN
    SELECT RAISE(ABORT, 'issue events are immutable');
END;

UPDATE monitoring_meta
SET value = '2'
WHERE key = 'schema_version';

COMMIT;
PRAGMA foreign_keys = ON;
"""


class MonitoringRegistry:
    """SQLite registry and create-only artifact store for one administrator."""

    def __init__(self, db_path: str | Path, *, artifact_root: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._install_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _install_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA_SQL)
            row = connection.execute(
                "SELECT value FROM monitoring_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO monitoring_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                return

            version = int(row["value"])
            if version == 1:
                connection.executescript(_MIGRATE_SCHEMA_V1_TO_V2_SQL)
                version = 2
            if version == 2:
                columns = {
                    str(column["name"])
                    for column in connection.execute("PRAGMA table_info(runs)")
                }
                add_runtime_profile = (
                    ""
                    if "runtime_profile_json" in columns
                    else (
                        "ALTER TABLE runs ADD COLUMN runtime_profile_json "
                        "TEXT NOT NULL DEFAULT '{}';"
                    )
                )
                connection.executescript(
                    f"""
                    PRAGMA foreign_keys = OFF;
                    BEGIN IMMEDIATE;

                    DROP TRIGGER IF EXISTS release_no_update;
                    DROP TRIGGER IF EXISTS release_no_delete;
                    DROP TABLE IF EXISTS release_manifests_v3;
                    CREATE TABLE release_manifests_v3 (
                        release_manifest_id TEXT PRIMARY KEY,
                        release_tag TEXT NOT NULL UNIQUE,
                        app_version TEXT NOT NULL,
                        manifest_version INTEGER NOT NULL,
                        lifecycle_status TEXT NOT NULL
                            CHECK (lifecycle_status = 'REGISTERED'),
                        runtime_bundle_digest TEXT NOT NULL,
                        bundle_relpath TEXT NOT NULL UNIQUE,
                        manifest_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO release_manifests_v3(
                        release_manifest_id, release_tag, app_version,
                        manifest_version, lifecycle_status,
                        runtime_bundle_digest, bundle_relpath, manifest_json,
                        created_at
                    )
                    SELECT
                        release_manifest_id, release_tag, app_version,
                        manifest_version, lifecycle_status,
                        runtime_bundle_digest, bundle_relpath, manifest_json,
                        created_at
                    FROM release_manifests;
                    DROP TABLE release_manifests;
                    ALTER TABLE release_manifests_v3
                        RENAME TO release_manifests;
                    CREATE TRIGGER release_no_update
                    BEFORE UPDATE ON release_manifests
                    BEGIN
                        SELECT RAISE(ABORT, 'registered releases are immutable');
                    END;
                    CREATE TRIGGER release_no_delete
                    BEFORE DELETE ON release_manifests
                    BEGIN
                        SELECT RAISE(ABORT, 'registered releases cannot be deleted');
                    END;

                    DROP TRIGGER IF EXISTS run_input_no_update;
                    {add_runtime_profile}
                    CREATE TRIGGER run_input_no_update
                    BEFORE UPDATE OF issue_id, case_contract_id,
                                     release_manifest_id, side,
                                     runtime_profile_json ON runs
                    BEGIN
                        SELECT RAISE(ABORT, 'run inputs are immutable');
                    END;
                    UPDATE monitoring_meta
                       SET value = '3'
                     WHERE key = 'schema_version';
                    COMMIT;
                    PRAGMA foreign_keys = ON;
                    """
                )
                version = 3
            if version != SCHEMA_VERSION:
                raise MonitoringContractError(
                    f"unsupported monitoring registry schema: {row['value']}"
                )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MonitoringContractError(
                    "monitoring registry migration left invalid references"
                )

    @staticmethod
    def _decode_row(row: sqlite3.Row, *json_fields: str) -> dict[str, Any]:
        result = dict(row)
        for field in json_fields:
            raw = result.get(field)
            result[field.removesuffix("_json")] = (
                strict_json_loads(raw) if raw is not None else None
            )
            result.pop(field, None)
        return result

    @staticmethod
    def _fetch_one(
        connection: sqlite3.Connection,
        sql: str,
        parameters: Sequence[Any],
        *,
        label: str,
    ) -> sqlite3.Row:
        row = connection.execute(sql, tuple(parameters)).fetchone()
        if row is None:
            raise MonitoringRecordNotFound(f"{label} not found")
        return row

    def _record_event(
        self,
        connection: sqlite3.Connection,
        *,
        issue_id: str,
        event_type: str,
        actor_user_id: str | None = None,
        reason: str | None = None,
        details: Mapping[str, Any] | None = None,
        before_revision: int | None = None,
        after_revision: int | None = None,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO issue_events(
                event_id, issue_id, event_type, actor_user_id, reason,
                details_json, before_revision, after_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _opaque_id("event"),
                issue_id,
                event_type,
                actor_user_id,
                reason,
                _canonical_json(dict(details or {})),
                before_revision,
                after_revision,
                created_at or _utc_now(),
            ),
        )

    def create_issue(
        self,
        *,
        source_receipt_id: str,
        reported_release_id: str,
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt_id = _nonempty_text(source_receipt_id, label="source_receipt_id")
        release_id = _nonempty_text(reported_release_id, label="reported_release_id")
        summary_body = _json_object(summary, label="summary")
        if not summary_body:
            raise MonitoringContractError("summary is required")
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM issues WHERE source_receipt_id = ?", (receipt_id,)
            ).fetchone()
            if existing is not None:
                decoded = self._decode_row(existing, "summary_json")
                if (
                    decoded["reported_release_id"] != release_id
                    or decoded["summary"] != summary_body
                ):
                    raise MonitoringContractError(
                        "source receipt already exists with different immutable content"
                    )
                return decoded
            issue_id = _opaque_id("issue")
            connection.execute(
                """
                INSERT INTO issues(
                    issue_id, source_receipt_id, reported_release_id, status,
                    summary_json, record_revision, created_at, updated_at
                ) VALUES (?, ?, ?, 'OPEN', ?, 1, ?, ?)
                """,
                (issue_id, receipt_id, release_id, _canonical_json(summary_body), now, now),
            )
            self._record_event(
                connection,
                issue_id=issue_id,
                event_type="CREATED",
                details={"source_receipt_id": receipt_id},
                after_revision=1,
                created_at=now,
            )
            row = self._fetch_one(
                connection,
                "SELECT * FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            return self._decode_row(row, "summary_json")

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            return self._decode_row(row, "summary_json")

    def list_issues(self, *, status: str | None = None) -> list[dict[str, Any]]:
        normalized_status = str(status).upper() if status is not None else None
        if normalized_status is not None and normalized_status not in ISSUE_STATUSES:
            raise MonitoringContractError(
                "status must be a supported Issue lifecycle state"
            )
        sql = "SELECT * FROM issues"
        parameters: tuple[Any, ...] = ()
        if normalized_status:
            sql += " WHERE status = ?"
            parameters = (normalized_status,)
        sql += " ORDER BY created_at ASC, issue_id ASC"
        with self._connect() as connection:
            return [
                self._decode_row(row, "summary_json")
                for row in connection.execute(sql, parameters).fetchall()
            ]

    def transition_issue(
        self,
        issue_id: str,
        *,
        target_status: str,
        reason: str,
        actor_user_id: str,
        expected_record_revision: int,
    ) -> dict[str, Any]:
        normalized_target = str(target_status).upper()
        if normalized_target not in ISSUE_STATUSES:
            raise MonitoringContractError(
                "target_status must be a supported Issue lifecycle state"
            )
        reason_text = _nonempty_text(reason, label="reason")
        actor = _nonempty_text(actor_user_id, label="actor_user_id")
        now = _utc_now()
        with self._transaction() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            current = dict(row)
            if int(current["record_revision"]) != int(expected_record_revision):
                raise RevisionConflictError(
                    "Issue record_revision changed; reload before updating"
                )
            if current["status"] == normalized_target:
                raise MonitoringContractError(f"Issue is already {normalized_target}")
            allowed_targets = ISSUE_STATUS_TRANSITIONS.get(
                str(current["status"]), frozenset()
            )
            if normalized_target not in allowed_targets:
                raise MonitoringContractError("illegal Issue status transition")
            next_revision = int(current["record_revision"]) + 1
            cursor = connection.execute(
                """
                UPDATE issues
                   SET status = ?, record_revision = ?, updated_at = ?
                 WHERE issue_id = ? AND record_revision = ?
                """,
                (
                    normalized_target,
                    next_revision,
                    now,
                    issue_id,
                    expected_record_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflictError(
                    "Issue record_revision changed; reload before updating"
                )
            self._record_event(
                connection,
                issue_id=issue_id,
                event_type=_ISSUE_EVENT_BY_TARGET[normalized_target],
                actor_user_id=actor,
                reason=reason_text,
                details={"from": current["status"], "to": normalized_target},
                before_revision=int(current["record_revision"]),
                after_revision=next_revision,
                created_at=now,
            )
            updated = self._fetch_one(
                connection,
                "SELECT * FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            return self._decode_row(updated, "summary_json")

    def record_raw_view(
        self,
        issue_id: str,
        *,
        actor_user_id: str,
        revealed_fields: Sequence[str],
    ) -> None:
        actor = _nonempty_text(actor_user_id, label="actor_user_id")
        fields = sorted({_nonempty_text(item, label="revealed field") for item in revealed_fields})
        with self._transaction() as connection:
            self._fetch_one(
                connection,
                "SELECT issue_id FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            self._record_event(
                connection,
                issue_id=issue_id,
                event_type="RAW_VIEWED",
                actor_user_id=actor,
                details={"revealed_fields": fields},
            )

    def list_issue_events(self, issue_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                self._decode_row(row, "details_json")
                for row in connection.execute(
                    """
                    SELECT * FROM issue_events
                     WHERE issue_id = ?
                     ORDER BY created_at ASC, rowid ASC
                    """,
                    (issue_id,),
                ).fetchall()
            ]

    @staticmethod
    def _fixture_body(
        *,
        question: str,
        reported_symptom: str,
        expected_behavior: str,
        typed_checks: Sequence[Mapping[str, Any]],
        manual_checks: Sequence[str] | None,
    ) -> dict[str, Any]:
        checks = [dict(item) for item in typed_checks]
        return {
            "schema_version": 1,
            "question": str(question),
            "reported_symptom": str(reported_symptom),
            "expected_behavior": str(expected_behavior),
            "typed_checks": checks,
            "manual_checks": [str(item) for item in (manual_checks or [])],
        }

    @staticmethod
    def _validate_fixture_body(body: Mapping[str, Any]) -> None:
        for key in ("question", "reported_symptom", "expected_behavior"):
            _nonempty_text(body.get(key), label=key)
        checks = body.get("typed_checks")
        if not isinstance(checks, list) or not checks:
            raise MonitoringContractError("typed_checks must contain at least one check")
        for check in checks:
            if not isinstance(check, Mapping):
                raise MonitoringContractError("typed check must be an object")
            check_type = str(check.get("type") or "")
            if check_type not in VALID_CHECK_TYPES:
                raise MonitoringContractError(f"unsupported typed check: {check_type}")
            if check_type != "CITATION_PRESENT" and not any(
                value not in (None, "", [])
                for key, value in check.items()
                if key != "type"
            ):
                raise MonitoringContractError(
                    f"typed check {check_type} has no expected value"
                )

    def create_fixture_revision(
        self,
        *,
        issue_id: str,
        question: str,
        reported_symptom: str,
        expected_behavior: str,
        typed_checks: Sequence[Mapping[str, Any]],
        manual_checks: Sequence[str] | None = None,
        predecessor_fixture_revision_id: str | None = None,
    ) -> dict[str, Any]:
        body = self._fixture_body(
            question=question,
            reported_symptom=reported_symptom,
            expected_behavior=expected_behavior,
            typed_checks=typed_checks,
            manual_checks=manual_checks,
        )
        fixture_id = _opaque_id("fixture")
        with self._transaction() as connection:
            self._fetch_one(
                connection,
                "SELECT issue_id FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            if predecessor_fixture_revision_id:
                self._fetch_one(
                    connection,
                    "SELECT fixture_revision_id FROM fixture_revisions WHERE fixture_revision_id = ?",
                    (predecessor_fixture_revision_id,),
                    label="Fixture revision",
                )
            connection.execute(
                """
                INSERT INTO fixture_revisions(
                    fixture_revision_id, issue_id, predecessor_fixture_revision_id,
                    lifecycle_status, body_json, created_at
                ) VALUES (?, ?, ?, 'DRAFT', ?, ?)
                """,
                (
                    fixture_id,
                    issue_id,
                    predecessor_fixture_revision_id,
                    _canonical_json(body),
                    _utc_now(),
                ),
            )
        return self.get_fixture_revision(fixture_id)

    def get_fixture_revision(self, fixture_revision_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM fixture_revisions WHERE fixture_revision_id = ?",
                (fixture_revision_id,),
                label="Fixture revision",
            )
            return self._decode_row(row, "body_json")

    def list_fixture_revisions(self, issue_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM fixture_revisions
                 WHERE issue_id = ?
                 ORDER BY created_at ASC, fixture_revision_id ASC
                """,
                (issue_id,),
            ).fetchall()
        return [self._decode_row(row, "body_json") for row in rows]

    def update_fixture_revision(
        self,
        fixture_revision_id: str,
        **updates: Any,
    ) -> dict[str, Any]:
        allowed = {
            "question",
            "reported_symptom",
            "expected_behavior",
            "typed_checks",
            "manual_checks",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise MonitoringContractError(f"unknown Fixture fields: {sorted(unknown)}")
        with self._transaction() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM fixture_revisions WHERE fixture_revision_id = ?",
                (fixture_revision_id,),
                label="Fixture revision",
            )
            if row["lifecycle_status"] != "DRAFT":
                raise ImmutableRecordError("READY fixture revisions are immutable")
            body = strict_json_loads(row["body_json"])
            body.update(updates)
            connection.execute(
                "UPDATE fixture_revisions SET body_json = ? WHERE fixture_revision_id = ?",
                (_canonical_json(body), fixture_revision_id),
            )
        return self.get_fixture_revision(fixture_revision_id)

    def mark_fixture_ready(self, fixture_revision_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM fixture_revisions WHERE fixture_revision_id = ?",
                (fixture_revision_id,),
                label="Fixture revision",
            )
            if row["lifecycle_status"] != "DRAFT":
                raise ImmutableRecordError("READY fixture revisions are immutable")
            body = strict_json_loads(row["body_json"])
            self._validate_fixture_body(body)
            digest = _json_digest(body)
            connection.execute(
                """
                UPDATE fixture_revisions
                   SET lifecycle_status = 'READY', fixture_digest = ?, ready_at = ?
                 WHERE fixture_revision_id = ?
                """,
                (digest, _utc_now(), fixture_revision_id),
            )
        return self.get_fixture_revision(fixture_revision_id)

    def revise_fixture(
        self,
        fixture_revision_id: str,
        **updates: Any,
    ) -> dict[str, Any]:
        original = self.get_fixture_revision(fixture_revision_id)
        if original["lifecycle_status"] != "READY":
            raise MonitoringContractError("only READY Fixture revisions can be revised")
        body = dict(original["body"])
        body.update(updates)
        return self.create_fixture_revision(
            issue_id=original["issue_id"],
            question=body["question"],
            reported_symptom=body["reported_symptom"],
            expected_behavior=body["expected_behavior"],
            typed_checks=body["typed_checks"],
            manual_checks=body.get("manual_checks") or [],
            predecessor_fixture_revision_id=fixture_revision_id,
        )

    def register_fixed_snapshot(
        self,
        *,
        fixed_snapshot_revision_id: str,
        bundle_relpath: str,
        bundle_digest: str,
        manifest: Mapping[str, Any],
        reader_contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        snapshot_id = _nonempty_text(
            fixed_snapshot_revision_id, label="fixed_snapshot_revision_id"
        )
        relpath = _nonempty_text(bundle_relpath, label="bundle_relpath")
        if Path(relpath).is_absolute() or ".." in Path(relpath).parts:
            raise MonitoringContractError("Snapshot bundle_relpath must stay relative")
        digest = _nonempty_text(bundle_digest, label="bundle_digest")
        if len(digest) != 64:
            raise MonitoringContractError("Snapshot bundle_digest must be sha256")
        manifest_body = _json_object(manifest, label="Snapshot manifest")
        reader_body = _json_object(reader_contract, label="Snapshot reader contract")
        now = _utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM fixed_snapshot_revisions WHERE fixed_snapshot_revision_id = ?",
                (snapshot_id,),
            ).fetchone()
            if existing is not None:
                decoded = self._decode_row(
                    existing, "manifest_json", "reader_contract_json"
                )
                if (
                    decoded["bundle_digest"] != digest
                    or decoded["manifest"] != manifest_body
                    or decoded["reader_contract"] != reader_body
                ):
                    raise ImmutableRecordError(
                        "fixed Snapshot identity cannot be replaced with different bytes"
                    )
                return decoded
            connection.execute(
                """
                INSERT INTO fixed_snapshot_revisions(
                    fixed_snapshot_revision_id, lifecycle_status, bundle_relpath,
                    bundle_digest, manifest_json, reader_contract_json, created_at
                ) VALUES (?, 'READY', ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    relpath,
                    digest,
                    _canonical_json(manifest_body),
                    _canonical_json(reader_body),
                    now,
                ),
            )
        return self.get_fixed_snapshot(snapshot_id)

    def get_fixed_snapshot(self, fixed_snapshot_revision_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM fixed_snapshot_revisions WHERE fixed_snapshot_revision_id = ?",
                (fixed_snapshot_revision_id,),
                label="FixedSnapshot revision",
            )
            return self._decode_row(row, "manifest_json", "reader_contract_json")

    def list_fixed_snapshots(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM fixed_snapshot_revisions
                 ORDER BY created_at ASC, fixed_snapshot_revision_id ASC
                """
            ).fetchall()
        return [
            self._decode_row(row, "manifest_json", "reader_contract_json")
            for row in rows
        ]

    def create_case_revision(
        self,
        *,
        issue_id: str,
        fixture_revision_id: str,
        fixed_snapshot_revision_id: str,
        fixed_clock: str | None,
        evaluator: Mapping[str, Any],
        reconstruction_lineage: Mapping[str, Any],
        predecessor_case_revision_id: str | None = None,
    ) -> dict[str, Any]:
        case_id = _opaque_id("case")
        evaluator_body = _json_object(evaluator, label="evaluator")
        lineage_body = _json_object(
            reconstruction_lineage, label="reconstruction_lineage"
        )
        with self._transaction() as connection:
            self._fetch_one(
                connection,
                "SELECT issue_id FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            fixture = self._fetch_one(
                connection,
                "SELECT issue_id FROM fixture_revisions WHERE fixture_revision_id = ?",
                (fixture_revision_id,),
                label="Fixture revision",
            )
            if fixture["issue_id"] != issue_id:
                raise MonitoringContractError("Fixture belongs to a different Issue")
            self._fetch_one(
                connection,
                "SELECT fixed_snapshot_revision_id FROM fixed_snapshot_revisions WHERE fixed_snapshot_revision_id = ?",
                (fixed_snapshot_revision_id,),
                label="FixedSnapshot revision",
            )
            if predecessor_case_revision_id:
                self._fetch_one(
                    connection,
                    "SELECT case_revision_id FROM reproduction_case_revisions WHERE case_revision_id = ?",
                    (predecessor_case_revision_id,),
                    label="Case revision",
                )
            connection.execute(
                """
                INSERT INTO reproduction_case_revisions(
                    case_revision_id, issue_id, predecessor_case_revision_id,
                    fixture_revision_id, fixed_snapshot_revision_id,
                    lifecycle_status, fixed_clock, evaluator_json,
                    reconstruction_lineage_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)
                """,
                (
                    case_id,
                    issue_id,
                    predecessor_case_revision_id,
                    fixture_revision_id,
                    fixed_snapshot_revision_id,
                    str(fixed_clock).strip() if fixed_clock else None,
                    _canonical_json(evaluator_body),
                    _canonical_json(lineage_body),
                    _utc_now(),
                ),
            )
        return self.get_case_revision(case_id)

    def get_case_revision(self, case_revision_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM reproduction_case_revisions WHERE case_revision_id = ?",
                (case_revision_id,),
                label="Case revision",
            )
            return self._decode_row(
                row, "evaluator_json", "reconstruction_lineage_json"
            )

    def get_case_by_contract(self, case_contract_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM reproduction_case_revisions WHERE case_contract_id = ?",
                (case_contract_id,),
                label="Case contract",
            )
            return self._decode_row(
                row, "evaluator_json", "reconstruction_lineage_json"
            )

    def list_case_revisions(self, issue_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM reproduction_case_revisions
                 WHERE issue_id = ?
                 ORDER BY created_at ASC, case_revision_id ASC
                """,
                (issue_id,),
            ).fetchall()
        return [
            self._decode_row(
                row, "evaluator_json", "reconstruction_lineage_json"
            )
            for row in rows
        ]

    def update_case_revision(
        self,
        case_revision_id: str,
        **updates: Any,
    ) -> dict[str, Any]:
        allowed = {
            "fixture_revision_id",
            "fixed_snapshot_revision_id",
            "fixed_clock",
            "evaluator",
            "reconstruction_lineage",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise MonitoringContractError(f"unknown Case fields: {sorted(unknown)}")
        column_values: dict[str, Any] = {}
        for key, value in updates.items():
            if key == "evaluator":
                column_values["evaluator_json"] = _canonical_json(
                    _json_object(value, label="evaluator")
                )
            elif key == "reconstruction_lineage":
                column_values["reconstruction_lineage_json"] = _canonical_json(
                    _json_object(value, label="reconstruction_lineage")
                )
            else:
                column_values[key] = value
        with self._transaction() as connection:
            row = self._fetch_one(
                connection,
                "SELECT lifecycle_status FROM reproduction_case_revisions WHERE case_revision_id = ?",
                (case_revision_id,),
                label="Case revision",
            )
            if row["lifecycle_status"] != "DRAFT":
                raise ImmutableRecordError("READY case revisions are immutable")
            if column_values:
                assignments = ", ".join(f"{key} = ?" for key in column_values)
                connection.execute(
                    f"UPDATE reproduction_case_revisions SET {assignments} WHERE case_revision_id = ?",
                    (*column_values.values(), case_revision_id),
                )
        return self.get_case_revision(case_revision_id)

    def revise_case(
        self,
        case_revision_id: str,
        **updates: Any,
    ) -> dict[str, Any]:
        original = self.get_case_revision(case_revision_id)
        if original["lifecycle_status"] != "READY":
            raise MonitoringContractError(
                "only READY Case revisions can be revised"
            )
        allowed = {
            "fixture_revision_id",
            "fixed_snapshot_revision_id",
            "fixed_clock",
            "evaluator",
            "reconstruction_lineage",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise MonitoringContractError(
                f"unknown Case fields: {sorted(unknown)}"
            )
        values = {
            "fixture_revision_id": original["fixture_revision_id"],
            "fixed_snapshot_revision_id": original[
                "fixed_snapshot_revision_id"
            ],
            "fixed_clock": original["fixed_clock"],
            "evaluator": original["evaluator"],
            "reconstruction_lineage": original["reconstruction_lineage"],
        }
        values.update(updates)
        return self.create_case_revision(
            issue_id=original["issue_id"],
            predecessor_case_revision_id=case_revision_id,
            **values,
        )

    @staticmethod
    def _lineage_ready(lineage: Mapping[str, Any]) -> tuple[str, str]:
        basis = str(lineage.get("basis") or "REPORT_DIAGNOSTICS").upper()
        if basis == "OPERATOR_DEFINED":
            if lineage.get("operator_scope_confirmed") is not True:
                raise MonitoringContractError(
                    "operator-defined lineage scope must be confirmed before Case READY"
                )
            _nonempty_text(
                lineage.get("operator_scope_reason"),
                label="operator-defined lineage reason",
            )
        elif basis != "REPORT_DIAGNOSTICS":
            raise MonitoringContractError("unsupported lineage basis")
        exceptions = lineage.get("exceptions") or []
        if not isinstance(exceptions, list):
            raise MonitoringContractError("lineage exceptions must be a list")
        kinds: set[str] = set()
        for item in exceptions:
            if not isinstance(item, Mapping):
                raise MonitoringContractError("lineage exception must be an object")
            if item.get("confirmed") is not True:
                raise MonitoringContractError(
                    "lineage exceptions must be confirmed before Case READY"
                )
            kind = str(item.get("kind") or "").upper()
            if kind not in {"CONTENT_DIFFERENT", "SUBSTITUTE", "MISSING"}:
                raise MonitoringContractError(f"unsupported lineage exception: {kind}")
            if kind in {"SUBSTITUTE", "MISSING"}:
                _nonempty_text(item.get("reason"), label="lineage exception reason")
            kinds.add(kind)
        explicit = str(lineage.get("evidence_qualifier") or "").upper()
        if basis == "OPERATOR_DEFINED":
            qualifier = "PARTIAL"
        elif "SUBSTITUTE" in kinds:
            qualifier = "SUBSTITUTE_INCLUDED"
        elif kinds:
            qualifier = "PARTIAL"
        elif explicit:
            qualifier = explicit
        else:
            qualifier = "EXACT"
        if qualifier not in {"EXACT", "PARTIAL", "SUBSTITUTE_INCLUDED"}:
            raise MonitoringContractError("invalid lineage evidence qualifier")
        return _json_digest(lineage), qualifier

    def mark_case_ready(
        self,
        case_revision_id: str,
        *,
        snapshot_available: bool,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM reproduction_case_revisions WHERE case_revision_id = ?",
                (case_revision_id,),
                label="Case revision",
            )
            if row["lifecycle_status"] != "DRAFT":
                raise ImmutableRecordError("READY case revisions are immutable")
            fixture = self._fetch_one(
                connection,
                "SELECT lifecycle_status, fixture_digest FROM fixture_revisions WHERE fixture_revision_id = ?",
                (row["fixture_revision_id"],),
                label="Fixture revision",
            )
            if fixture["lifecycle_status"] != "READY":
                raise MonitoringContractError("Fixture must be READY before Case READY")
            if not snapshot_available:
                raise MonitoringContractError(
                    "Snapshot must be available before Case READY"
                )
            evaluator = strict_json_loads(row["evaluator_json"])
            if not evaluator:
                raise MonitoringContractError("evaluator is required before Case READY")
            lineage = strict_json_loads(row["reconstruction_lineage_json"])
            lineage_digest, qualifier = self._lineage_ready(lineage)
            contract_body = {
                "schema_version": 1,
                "issue_id": row["issue_id"],
                "fixture_revision_id": row["fixture_revision_id"],
                "fixture_digest": fixture["fixture_digest"],
                "fixed_snapshot_revision_id": row["fixed_snapshot_revision_id"],
                "fixed_clock": row["fixed_clock"],
                "evaluator": evaluator,
                "lineage_proof_digest": lineage_digest,
                "evidence_qualifier": qualifier,
            }
            case_contract_id = _json_digest(contract_body)
            now = _utc_now()
            connection.execute(
                """
                UPDATE reproduction_case_revisions
                   SET lifecycle_status = 'READY', lineage_proof_digest = ?,
                       evidence_qualifier = ?, case_contract_id = ?, ready_at = ?
                 WHERE case_revision_id = ?
                """,
                (
                    lineage_digest,
                    qualifier,
                    case_contract_id,
                    now,
                    case_revision_id,
                ),
            )
            issue = self._fetch_one(
                connection,
                "SELECT record_revision FROM issues WHERE issue_id = ?",
                (row["issue_id"],),
                label="Issue",
            )
            next_revision = int(issue["record_revision"]) + 1
            connection.execute(
                """
                UPDATE issues
                   SET current_case_contract_id = ?, record_revision = ?, updated_at = ?
                 WHERE issue_id = ?
                """,
                (case_contract_id, next_revision, now, row["issue_id"]),
            )
            self._record_event(
                connection,
                issue_id=row["issue_id"],
                event_type="CASE_READY",
                details={
                    "case_revision_id": case_revision_id,
                    "case_contract_id": case_contract_id,
                },
                before_revision=int(issue["record_revision"]),
                after_revision=next_revision,
                created_at=now,
            )
        return self.get_case_revision(case_revision_id)

    def register_release_manifest(
        self,
        *,
        release_manifest_id: str,
        release_tag: str,
        app_version: str,
        manifest_version: int,
        runtime_bundle_digest: str,
        bundle_relpath: str,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        release_id = _nonempty_text(release_manifest_id, label="release_manifest_id")
        tag = _nonempty_text(release_tag, label="release_tag")
        version = _nonempty_text(app_version, label="app_version")
        if tag == "v0.6.0" or version == "0.6.0":
            raise MonitoringContractError("v0.6.0 is not an official Release record")
        normalized = tag.removeprefix("v")
        if normalized != version:
            raise MonitoringContractError("release tag and app_version must match")
        normalized_manifest_version = int(manifest_version)
        if normalized_manifest_version not in {1, 2}:
            raise MonitoringContractError("manifest_version must be 1 or 2")
        digest = _nonempty_text(runtime_bundle_digest, label="runtime_bundle_digest")
        if len(digest) != 64:
            raise MonitoringContractError("runtime_bundle_digest must be sha256")
        relpath = _nonempty_text(bundle_relpath, label="bundle_relpath")
        if Path(relpath).is_absolute() or ".." in Path(relpath).parts:
            raise MonitoringContractError("Release bundle_relpath must stay relative")
        manifest_body = _json_object(manifest, label="release manifest")
        declared_schema = manifest_body.get("schema_version")
        if declared_schema is not None:
            try:
                if int(declared_schema) != normalized_manifest_version:
                    raise MonitoringContractError(
                        "release manifest schema does not match manifest_version"
                    )
            except (TypeError, ValueError) as exc:
                raise MonitoringContractError(
                    "release manifest schema is invalid"
                ) from exc
        if normalized_manifest_version == 2:
            manifest_app_version = str(
                manifest_body.get("app_version") or ""
            ).removeprefix("v")
            git_revision = str(
                manifest_body.get("git_revision") or ""
            ).casefold()
            expected_release_id = _json_digest(
                {
                    "identity_schema_version": 2,
                    "app_version": version,
                    "git_revision": git_revision,
                }
            )
            if (
                manifest_app_version != version
                or not _FULL_GIT_REVISION_RE.fullmatch(git_revision)
                or release_id != expected_release_id
            ):
                raise MonitoringContractError(
                    "v2 Release identity must derive from app version and Git commit"
                )
        if int(manifest_body.get("runner_contract_version") or 0) != 1:
            raise MonitoringContractError("runner_contract_version must be 1")
        with self._transaction() as connection:
            same_tag = connection.execute(
                "SELECT * FROM release_manifests WHERE release_tag = ?", (tag,)
            ).fetchone()
            if same_tag is not None:
                decoded = self._decode_row(same_tag, "manifest_json")
                if (
                    decoded["release_manifest_id"] != release_id
                    or decoded["runtime_bundle_digest"] != digest
                    or decoded["manifest"] != manifest_body
                ):
                    raise ImmutableRecordError(
                        "the same release version cannot be registered with different bytes"
                    )
                return decoded
            connection.execute(
                """
                INSERT INTO release_manifests(
                    release_manifest_id, release_tag, app_version, manifest_version,
                    lifecycle_status, runtime_bundle_digest, bundle_relpath,
                    manifest_json, created_at
                ) VALUES (?, ?, ?, ?, 'REGISTERED', ?, ?, ?, ?)
                """,
                (
                    release_id,
                    tag,
                    version,
                    normalized_manifest_version,
                    digest,
                    relpath,
                    _canonical_json(manifest_body),
                    _utc_now(),
                ),
            )
        return self.get_release_manifest(release_id)

    def get_release_manifest(self, release_manifest_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM release_manifests WHERE release_manifest_id = ?",
                (release_manifest_id,),
                label="ReleaseManifest",
            )
            return self._decode_row(row, "manifest_json")

    def find_release_by_version(self, app_version: str) -> dict[str, Any] | None:
        normalized = str(app_version).strip().removeprefix("v")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM release_manifests WHERE app_version = ?",
                (normalized,),
            ).fetchone()
            return self._decode_row(row, "manifest_json") if row is not None else None

    def list_release_manifests(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM release_manifests ORDER BY app_version, release_manifest_id"
            ).fetchall()
            return [self._decode_row(row, "manifest_json") for row in rows]

    def queue_run(
        self,
        *,
        issue_id: str,
        case_contract_id: str,
        release_manifest_id: str,
        side: str,
        runtime_profile: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_side = str(side).upper()
        if normalized_side not in VALID_RUN_SIDES:
            raise MonitoringContractError("Run side must be BASELINE or CANDIDATE")
        profile = _json_object(
            runtime_profile or {},
            label="runtime profile",
        )
        run_id = _opaque_id("run")
        with self._transaction() as connection:
            issue = self._fetch_one(
                connection,
                "SELECT * FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            case = self._fetch_one(
                connection,
                "SELECT issue_id, lifecycle_status FROM reproduction_case_revisions WHERE case_contract_id = ?",
                (case_contract_id,),
                label="Case contract",
            )
            if case["issue_id"] != issue_id or case["lifecycle_status"] != "READY":
                raise MonitoringContractError("Run requires this Issue's READY Case")
            release = self._fetch_one(
                connection,
                "SELECT release_manifest_id, app_version FROM release_manifests WHERE release_manifest_id = ?",
                (release_manifest_id,),
                label="ReleaseManifest",
            )
            if (
                normalized_side == "BASELINE"
                and issue["reported_release_id"]
                not in {
                    release_manifest_id,
                    f"release-v{release['app_version']}",
                }
            ):
                raise MonitoringContractError(
                    "Baseline must use the Issue reported release"
                )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, issue_id, case_contract_id, release_manifest_id,
                    side, runtime_profile_json, execution_status, queued_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?)
                """,
                (
                    run_id,
                    issue_id,
                    case_contract_id,
                    release_manifest_id,
                    normalized_side,
                    _canonical_json(profile),
                    _utc_now(),
                ),
            )
        return self._get_run_row(run_id)

    def _get_run_row(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
                label="Run",
            )
            return self._decode_row(row, "runtime_profile_json")

    def start_run(self, run_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            row = self._fetch_one(
                connection,
                "SELECT execution_status FROM runs WHERE run_id = ?",
                (run_id,),
                label="Run",
            )
            if row["execution_status"] != "QUEUED":
                raise ImmutableRecordError("Run can only start from QUEUED")
            connection.execute(
                """
                UPDATE runs SET execution_status = 'RUNNING', started_at = ?
                 WHERE run_id = ?
                """,
                (_utc_now(), run_id),
            )
        return self._get_run_row(run_id)

    def _run_artifact_path(self, run_id: str) -> Path:
        target = (self.artifact_root / "runs" / f"{run_id}.json").resolve()
        try:
            target.relative_to(self.artifact_root)
        except ValueError as exc:
            raise MonitoringContractError("Run artifact escaped managed root") from exc
        return target

    @staticmethod
    def _validate_run_artifact(
        artifact: Mapping[str, Any], *, execution_status: str
    ) -> dict[str, Any]:
        body = _json_object(artifact, label="Run artifact")
        if execution_status == "SUCCEEDED":
            required = {
                "raw_answer",
                "evidence_refs",
                "route_summary",
                "check_result",
                "runtime_profile",
                "latency_ms",
            }
            missing = sorted(required - set(body))
            if missing:
                raise MonitoringContractError(
                    f"successful Run artifact is missing: {', '.join(missing)}"
                )
            if not isinstance(body["raw_answer"], str):
                raise MonitoringContractError("raw_answer must be text")
            if not isinstance(body["evidence_refs"], list):
                raise MonitoringContractError("evidence_refs must be a list")
            if len(body["evidence_refs"]) > 100:
                raise MonitoringContractError("evidence_refs is too large")
            for index, evidence in enumerate(body["evidence_refs"]):
                if not isinstance(evidence, Mapping):
                    raise MonitoringContractError(
                        f"evidence_refs[{index}] must be an object"
                    )
                allowed = {
                    "role",
                    "source_uid",
                    "source_sha256",
                    "chunk_uid",
                    "chunk_sha256",
                    "locator",
                    "rank",
                }
                if set(evidence) - allowed:
                    raise MonitoringContractError(
                        f"evidence_refs[{index}] has unknown fields"
                    )
                role = str(evidence.get("role") or "")
                source_uid = str(evidence.get("source_uid") or "")
                source_sha256 = str(evidence.get("source_sha256") or "")
                if role not in _EVIDENCE_ROLES:
                    raise MonitoringContractError(
                        f"evidence_refs[{index}] role is invalid"
                    )
                if not _SHA256_RE.fullmatch(source_uid) or not _SHA256_RE.fullmatch(
                    source_sha256
                ):
                    raise MonitoringContractError(
                        f"evidence_refs[{index}] source identity is invalid"
                    )
                chunk_uid = evidence.get("chunk_uid")
                chunk_sha256 = evidence.get("chunk_sha256")
                if chunk_uid is not None and not _SHA256_RE.fullmatch(str(chunk_uid)):
                    raise MonitoringContractError(
                        f"evidence_refs[{index}] chunk_uid is invalid"
                    )
                if chunk_sha256 is not None and not _SHA256_RE.fullmatch(
                    str(chunk_sha256)
                ):
                    raise MonitoringContractError(
                        f"evidence_refs[{index}] chunk_sha256 is invalid"
                    )
                rank = evidence.get("rank")
                if role == "OBSERVED_RESULT" and (
                    isinstance(rank, bool)
                    or not isinstance(rank, int)
                    or rank < 1
                ):
                    raise MonitoringContractError(
                        f"evidence_refs[{index}] rank is required"
                    )
                locator = evidence.get("locator")
                if locator is not None:
                    locator_text = str(locator)
                    if (
                        not locator_text
                        or len(locator_text.encode("utf-8")) > 256
                        or "\\" in locator_text
                        or locator_text.startswith("/")
                        or re.match(r"^[A-Za-z]:", locator_text)
                    ):
                        raise MonitoringContractError(
                            f"evidence_refs[{index}] locator is unsafe"
                        )
            for field in ("route_summary", "check_result", "runtime_profile"):
                if not isinstance(body[field], Mapping):
                    raise MonitoringContractError(f"{field} must be an object")
            latency = body["latency_ms"]
            if isinstance(latency, bool) or not isinstance(latency, (int, float)):
                raise MonitoringContractError("latency_ms must be numeric")
            if float(latency) < 0:
                raise MonitoringContractError("latency_ms cannot be negative")
            invalid_reason = body.get("invalid_reason")
            if invalid_reason is not None and (
                not isinstance(invalid_reason, str)
                or not invalid_reason.strip()
                or len(invalid_reason) > 1000
            ):
                raise MonitoringContractError("invalid_reason is invalid")
        return body

    @staticmethod
    def _atomic_create(path: Path, body: Mapping[str, Any]) -> tuple[Path, str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (_canonical_json(dict(body)) + "\n").encode("utf-8")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise ImmutableRecordError(
                    f"artifact already exists and cannot be overwritten: {path.name}"
                ) from exc
            finally:
                temporary_path.unlink(missing_ok=True)
                temporary_path = None
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return path, _sha256_bytes(content)

    def finish_run(
        self,
        run_id: str,
        *,
        execution_status: str,
        validity: str,
        artifact: Mapping[str, Any],
    ) -> dict[str, Any]:
        status = str(execution_status).upper()
        normalized_validity = str(validity).upper()
        if status not in TERMINAL_RUN_STATUSES:
            raise MonitoringContractError("Run terminal status is invalid")
        if normalized_validity not in {"VALID", "INVALID"}:
            raise MonitoringContractError("Run validity must be VALID or INVALID")
        if status != "SUCCEEDED" and normalized_validity == "VALID":
            raise MonitoringContractError("only SUCCEEDED Runs can be VALID")
        body = self._validate_run_artifact(artifact, execution_status=status)
        current = self._get_run_row(run_id)
        if current["execution_status"] != "RUNNING":
            raise ImmutableRecordError("terminal Runs are immutable")
        artifact_body = {
            "schema_version": 1,
            "run_id": run_id,
            "issue_id": current["issue_id"],
            "case_contract_id": current["case_contract_id"],
            "release_manifest_id": current["release_manifest_id"],
            "side": current["side"],
            "execution_status": status,
            "validity": normalized_validity,
            **body,
        }
        target, digest = self._atomic_create(
            self._run_artifact_path(run_id), artifact_body
        )
        relative = target.relative_to(self.artifact_root).as_posix()
        try:
            with self._transaction() as connection:
                row = self._fetch_one(
                    connection,
                    "SELECT execution_status FROM runs WHERE run_id = ?",
                    (run_id,),
                    label="Run",
                )
                if row["execution_status"] != "RUNNING":
                    raise ImmutableRecordError("terminal Runs are immutable")
                connection.execute(
                    """
                    UPDATE runs
                       SET execution_status = ?, validity = ?, artifact_relpath = ?,
                           artifact_digest = ?, completed_at = ?
                     WHERE run_id = ?
                    """,
                    (status, normalized_validity, relative, digest, _utc_now(), run_id),
                )
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self._get_run_row(run_id)
        if row["artifact_relpath"] is None:
            return row
        path = (self.artifact_root / row["artifact_relpath"]).resolve()
        try:
            path.relative_to(self.artifact_root)
        except ValueError as exc:
            raise ImmutableRecordError("Run artifact escaped managed root") from exc
        if not path.is_file():
            raise ImmutableRecordError("Run artifact is missing")
        content = path.read_bytes()
        if _sha256_bytes(content) != row["artifact_digest"]:
            raise ImmutableRecordError("Run artifact digest mismatch")
        artifact = strict_json_loads(content.decode("utf-8"))
        if artifact.get("run_id") != run_id:
            raise ImmutableRecordError("Run artifact identity mismatch")
        return {**row, "artifact_path": str(path), "artifact": artifact}

    def list_runs(
        self,
        *,
        issue_id: str,
        case_contract_id: str | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["issue_id = ?"]
        parameters: list[Any] = [issue_id]
        if case_contract_id:
            clauses.append("case_contract_id = ?")
            parameters.append(case_contract_id)
        if side:
            normalized = str(side).upper()
            if normalized not in VALID_RUN_SIDES:
                raise MonitoringContractError("Run side is invalid")
            clauses.append("side = ?")
            parameters.append(normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs WHERE {' AND '.join(clauses)} ORDER BY queued_at ASC, run_id ASC",
                tuple(parameters),
            ).fetchall()
        return [self._decode_row(row, "runtime_profile_json") for row in rows]

    def create_comparison(
        self,
        *,
        issue_id: str,
        baseline_run_ids: Sequence[str],
        candidate_run_ids: Sequence[str],
        verdict: str,
        note: str,
        actor_user_id: str,
        supersedes_comparison_id: str | None = None,
    ) -> dict[str, Any]:
        baseline_ids = list(dict.fromkeys(str(item) for item in baseline_run_ids))
        candidate_ids = list(dict.fromkeys(str(item) for item in candidate_run_ids))
        if not baseline_ids or not candidate_ids:
            raise MonitoringContractError(
                "Comparison requires Baseline and Candidate Runs"
            )
        normalized_verdict = str(verdict).upper()
        if normalized_verdict not in VALID_COMPARISON_VERDICTS:
            raise MonitoringContractError("Comparison verdict is invalid")
        note_text = _nonempty_text(note, label="Comparison note")
        actor = _nonempty_text(actor_user_id, label="actor_user_id")
        comparison_id = _opaque_id("comparison")
        now = _utc_now()
        with self._transaction() as connection:
            issue = self._fetch_one(
                connection,
                "SELECT * FROM issues WHERE issue_id = ?",
                (issue_id,),
                label="Issue",
            )
            selected: list[sqlite3.Row] = []
            for run_id in baseline_ids + candidate_ids:
                selected.append(
                    self._fetch_one(
                        connection,
                        "SELECT * FROM runs WHERE run_id = ?",
                        (run_id,),
                        label="Run",
                    )
                )
            if any(row["issue_id"] != issue_id for row in selected):
                raise MonitoringContractError("Comparison Run belongs to another Issue")
            case_ids = {row["case_contract_id"] for row in selected}
            if len(case_ids) != 1:
                raise MonitoringContractError(
                    "Comparison Runs must use the same case_contract_id"
                )
            if any(
                row["execution_status"] != "SUCCEEDED" or row["validity"] != "VALID"
                for row in selected
            ):
                raise MonitoringContractError(
                    "Comparison only accepts SUCCEEDED + VALID Runs"
                )
            for row in selected[: len(baseline_ids)]:
                if row["side"] != "BASELINE":
                    raise MonitoringContractError("Baseline selection has a Candidate Run")
            for row in selected[len(baseline_ids) :]:
                if row["side"] != "CANDIDATE":
                    raise MonitoringContractError("Candidate selection has a Baseline Run")
            case_contract_id = next(iter(case_ids))
            if supersedes_comparison_id:
                predecessor = self._fetch_one(
                    connection,
                    "SELECT * FROM comparisons WHERE comparison_id = ?",
                    (supersedes_comparison_id,),
                    label="Comparison",
                )
                if (
                    predecessor["issue_id"] != issue_id
                    or predecessor["case_contract_id"] != case_contract_id
                ):
                    raise MonitoringContractError(
                        "superseded Comparison must belong to the same Issue and Case"
                    )
                if issue["current_comparison_id"] != supersedes_comparison_id:
                    raise MonitoringContractError(
                        "rejudgment must supersede the latest Comparison"
                    )
            elif issue["current_comparison_id"] is not None:
                raise MonitoringContractError(
                    "a later judgment must supersede the current Comparison"
                )
            record_body = {
                "schema_version": 1,
                "comparison_id": comparison_id,
                "issue_id": issue_id,
                "case_contract_id": case_contract_id,
                "baseline_run_ids": baseline_ids,
                "candidate_run_ids": candidate_ids,
                "verdict": normalized_verdict,
                "note": note_text,
                "actor_user_id": actor,
                "supersedes_comparison_id": supersedes_comparison_id,
                "created_at": now,
            }
            digest = _json_digest(record_body)
            connection.execute(
                """
                INSERT INTO comparisons(
                    comparison_id, issue_id, case_contract_id,
                    baseline_run_ids_json, candidate_run_ids_json, verdict,
                    note, actor_user_id, supersedes_comparison_id,
                    record_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison_id,
                    issue_id,
                    case_contract_id,
                    _canonical_json(baseline_ids),
                    _canonical_json(candidate_ids),
                    normalized_verdict,
                    note_text,
                    actor,
                    supersedes_comparison_id,
                    digest,
                    now,
                ),
            )
            next_revision = int(issue["record_revision"]) + 1
            connection.execute(
                """
                UPDATE issues
                   SET current_comparison_id = ?, record_revision = ?, updated_at = ?
                 WHERE issue_id = ?
                """,
                (comparison_id, next_revision, now, issue_id),
            )
            self._record_event(
                connection,
                issue_id=issue_id,
                event_type="COMPARISON_CREATED",
                actor_user_id=actor,
                details={
                    "comparison_id": comparison_id,
                    "verdict": normalized_verdict,
                    "supersedes_comparison_id": supersedes_comparison_id,
                },
                before_revision=int(issue["record_revision"]),
                after_revision=next_revision,
                created_at=now,
            )
        return self.get_comparison(comparison_id)

    def get_comparison(self, comparison_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_one(
                connection,
                "SELECT * FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
                label="Comparison",
            )
            result = self._decode_row(
                row, "baseline_run_ids_json", "candidate_run_ids_json"
            )
        record_body = {
            "schema_version": 1,
            "comparison_id": result["comparison_id"],
            "issue_id": result["issue_id"],
            "case_contract_id": result["case_contract_id"],
            "baseline_run_ids": result["baseline_run_ids"],
            "candidate_run_ids": result["candidate_run_ids"],
            "verdict": result["verdict"],
            "note": result["note"],
            "actor_user_id": result["actor_user_id"],
            "supersedes_comparison_id": result["supersedes_comparison_id"],
            "created_at": result["created_at"],
        }
        if _json_digest(record_body) != result["record_digest"]:
            raise ImmutableRecordError("Comparison record digest mismatch")
        return result

    def list_comparisons(self, issue_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            ids = [
                row["comparison_id"]
                for row in connection.execute(
                    """
                    SELECT comparison_id FROM comparisons
                     WHERE issue_id = ?
                     ORDER BY created_at ASC, comparison_id ASC
                    """,
                    (issue_id,),
                ).fetchall()
            ]
        return [self.get_comparison(comparison_id) for comparison_id in ids]

    def update_comparison(self, comparison_id: str, **updates: Any) -> None:
        del updates
        self.get_comparison(comparison_id)
        raise ImmutableRecordError(
            "Comparisons are immutable; create a superseding Comparison"
        )

    def derive_issue_progress(self, issue_id: str) -> dict[str, str]:
        issue = self.get_issue(issue_id)
        case_contract_id = issue.get("current_case_contract_id")
        if not case_contract_id:
            return {
                "reproduction": "NOT_PREPARED",
                "comparison": "NOT_COMPARED",
                "next_action": "PREPARE_CASE",
            }
        runs = self.list_runs(issue_id=issue_id, case_contract_id=case_contract_id)
        baseline_valid = [
            row
            for row in runs
            if row["side"] == "BASELINE"
            and row["execution_status"] == "SUCCEEDED"
            and row["validity"] == "VALID"
        ]
        if not baseline_valid:
            return {
                "reproduction": "NOT_OBSERVED",
                "comparison": "NOT_COMPARED",
                "next_action": "RUN_BASELINE",
            }
        reproduced = any(
            bool((self.get_run(row["run_id"])["artifact"].get("check_result") or {}).get("reproduced"))
            for row in baseline_valid
        )
        reproduction = "REPRODUCED" if reproduced else "NOT_OBSERVED"
        candidate_valid = [
            row
            for row in runs
            if row["side"] == "CANDIDATE"
            and row["execution_status"] == "SUCCEEDED"
            and row["validity"] == "VALID"
        ]
        if not candidate_valid:
            return {
                "reproduction": reproduction,
                "comparison": "NOT_COMPARED",
                "next_action": (
                    "RUN_CANDIDATE" if reproduced else "REVIEW_OR_REPEAT_BASELINE"
                ),
            }
        comparison_id = issue.get("current_comparison_id")
        if not comparison_id:
            return {
                "reproduction": reproduction,
                "comparison": "NOT_COMPARED",
                "next_action": "COMPARE_RUNS",
            }
        comparison = self.get_comparison(comparison_id)
        next_action = {
            "IMPROVED": "CLOSE_ISSUE",
            "INCONCLUSIVE": "RUN_AGAIN_OR_REJUDGE",
            "NOT_IMPROVED": "IMPROVE_AND_RERUN",
            "REGRESSED": "FIX_REGRESSION",
        }[comparison["verdict"]]
        return {
            "reproduction": reproduction,
            "comparison": comparison["verdict"],
            "next_action": next_action,
        }


__all__ = [
    "ImmutableRecordError",
    "MonitoringContractError",
    "MonitoringRecordNotFound",
    "MonitoringRegistry",
    "MonitoringRegistryError",
    "RevisionConflictError",
]
