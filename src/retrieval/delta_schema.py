'''Optional catalog extension for continuously published retrieval deltas.

The native V2 base catalog remains readable without this extension.  Writers
install it before the first delta publication, while readers treat an entirely
absent extension as an empty delta chain and reject partially installed state.
'''

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from src.retrieval.schema import MAX_SIGNED_INT64, SchemaError, configure_catalog_storage


DELTA_TABLES = frozenset(
    {
        'retrieval_delta_segments',
        'retrieval_delta_reports',
        'retrieval_delta_membership',
    }
)
DELTA_VIEWS = frozenset({'active_vector_membership'})
DELTA_GC_TABLES = frozenset({'retrieval_delta_artifact_gc'})
DELTA_INDEXES = frozenset(
    {
        'idx_retrieval_delta_segments_active',
        'idx_retrieval_delta_reports_path',
        'idx_retrieval_delta_membership_chunk',
    }
)
DELTA_TRIGGERS = frozenset(
    {
        'retrieval_delta_segments_immutable_fields',
        'retrieval_delta_segments_state_transition',
        'retrieval_delta_reports_validate_insert',
        'retrieval_delta_reports_no_update',
        'retrieval_delta_reports_no_delete',
        'retrieval_delta_membership_validate_insert',
        'retrieval_delta_membership_no_update',
        'retrieval_delta_membership_no_delete',
        'retrieval_delta_segments_validate_ready',
        'retrieval_delta_segments_no_delete',
    }
)
DELTA_GC_TRIGGERS = frozenset(
    {
        'retrieval_delta_artifact_gc_validate_insert',
        'retrieval_delta_artifact_gc_no_update',
        'retrieval_delta_artifact_gc_no_delete',
    }
)


_TABLE_DDL = (
    f'''
    CREATE TABLE IF NOT EXISTS retrieval_delta_segments (
        segment_id TEXT PRIMARY KEY
            CHECK (
                length(segment_id) = 64
                AND segment_id = lower(segment_id)
                AND segment_id NOT GLOB '*[^0-9a-f]*'
            ),
        base_snapshot_id TEXT NOT NULL,
        base_publication_generation INTEGER NOT NULL
            CHECK (base_publication_generation >= 0),
        sequence INTEGER NOT NULL
            CHECK (sequence > 0 AND sequence <= {MAX_SIGNED_INT64}),
        relative_path TEXT,
        file_sha256 TEXT,
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        dimension INTEGER NOT NULL CHECK (dimension > 0),
        metric TEXT NOT NULL CHECK (metric IN ('l2', 'inner_product')),
        ntotal INTEGER NOT NULL
            CHECK (ntotal >= 0 AND ntotal <= {MAX_SIGNED_INT64}),
        state TEXT NOT NULL DEFAULT 'staged'
            CHECK (state IN ('staged', 'ready', 'failed', 'compacted')),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        state_changed_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(state_changed_at) > 0),
        CHECK (
            segment_id <> base_snapshot_id
            AND
            (
                ntotal = 0
                AND relative_path IS NULL
                AND file_sha256 IS NULL
                AND size_bytes = 0
            )
            OR
            (
                ntotal > 0
                AND relative_path IS NOT NULL
                AND length(relative_path) > 0
                AND relative_path NOT LIKE '/%'
                AND relative_path NOT GLOB '[A-Za-z]:*'
                AND instr(relative_path, char(0)) = 0
                AND instr(relative_path, '\\') = 0
                AND instr(relative_path, '//') = 0
                AND relative_path NOT LIKE '%/'
                AND relative_path <> '.'
                AND relative_path NOT LIKE './%'
                AND relative_path NOT LIKE '%/./%'
                AND relative_path NOT LIKE '%/.'
                AND relative_path <> '..'
                AND relative_path NOT LIKE '%/../%'
                AND relative_path NOT LIKE '../%'
                AND relative_path NOT LIKE '%/..'
                AND length(file_sha256) = 64
                AND file_sha256 = lower(file_sha256)
                AND file_sha256 NOT GLOB '*[^0-9a-f]*'
                AND size_bytes > 0
            )
        ),
        UNIQUE (base_snapshot_id, base_publication_generation, sequence),
        UNIQUE (relative_path),
        FOREIGN KEY (base_snapshot_id) REFERENCES vector_snapshots(snapshot_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS retrieval_delta_reports (
        segment_id TEXT NOT NULL,
        canonical_relative_path TEXT NOT NULL
            CHECK (
                length(canonical_relative_path) > 0
                AND canonical_relative_path NOT LIKE '/%'
                AND canonical_relative_path NOT GLOB '[A-Za-z]:*'
                AND instr(canonical_relative_path, char(0)) = 0
                AND instr(canonical_relative_path, '\\') = 0
                AND instr(canonical_relative_path, '//') = 0
                AND canonical_relative_path NOT LIKE '%/'
                AND canonical_relative_path <> '.'
                AND canonical_relative_path NOT LIKE './%'
                AND canonical_relative_path NOT LIKE '%/./%'
                AND canonical_relative_path NOT LIKE '%/.'
                AND canonical_relative_path <> '..'
                AND canonical_relative_path NOT LIKE '../%'
                AND canonical_relative_path NOT LIKE '%/../%'
                AND canonical_relative_path NOT LIKE '%/..'
            ),
        action TEXT NOT NULL CHECK (action IN ('upsert', 'delete', 'failed')),
        report_uid TEXT,
        reason_code TEXT CHECK (
            reason_code IS NULL
            OR (
                length(reason_code) > 0
                AND reason_code = lower(reason_code)
                AND reason_code NOT GLOB '*[^a-z0-9._-]*'
            )
        ),
        PRIMARY KEY (segment_id, canonical_relative_path),
        CHECK (
            (action = 'upsert' AND report_uid IS NOT NULL AND reason_code IS NULL)
            OR (action = 'delete' AND report_uid IS NULL AND reason_code IS NULL)
            OR (action = 'failed' AND report_uid IS NOT NULL AND reason_code IS NOT NULL)
        ),
        FOREIGN KEY (segment_id)
            REFERENCES retrieval_delta_segments(segment_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (report_uid) REFERENCES reports(report_uid)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) WITHOUT ROWID
    ''',
    f'''
    CREATE TABLE IF NOT EXISTS retrieval_delta_membership (
        segment_id TEXT NOT NULL,
        chunk_uid TEXT NOT NULL,
        faiss_id INTEGER NOT NULL
            CHECK (faiss_id > 0 AND faiss_id <= {MAX_SIGNED_INT64}),
        PRIMARY KEY (segment_id, chunk_uid),
        UNIQUE (segment_id, faiss_id),
        FOREIGN KEY (segment_id)
            REFERENCES retrieval_delta_segments(segment_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (chunk_uid) REFERENCES retrieval_chunks(chunk_uid)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) WITHOUT ROWID
    ''',
)


_GC_TABLE_DDL = (
    '''
    CREATE TABLE IF NOT EXISTS retrieval_delta_artifact_gc (
        segment_id TEXT PRIMARY KEY,
        collected_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(collected_at) > 0),
        FOREIGN KEY (segment_id)
            REFERENCES retrieval_delta_segments(segment_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) WITHOUT ROWID
    ''',
)


_INDEX_DDL = (
    '''CREATE INDEX IF NOT EXISTS idx_retrieval_delta_segments_active
       ON retrieval_delta_segments(
           base_snapshot_id, base_publication_generation, state, sequence
       )''',
    '''CREATE INDEX IF NOT EXISTS idx_retrieval_delta_reports_path
       ON retrieval_delta_reports(canonical_relative_path, segment_id)''',
    '''CREATE INDEX IF NOT EXISTS idx_retrieval_delta_membership_chunk
       ON retrieval_delta_membership(chunk_uid)''',
)


_TRIGGER_DDL = (
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_segments_immutable_fields
    BEFORE UPDATE ON retrieval_delta_segments
    WHEN NEW.segment_id IS NOT OLD.segment_id
      OR NEW.base_snapshot_id IS NOT OLD.base_snapshot_id
      OR NEW.base_publication_generation IS NOT OLD.base_publication_generation
      OR NEW.sequence IS NOT OLD.sequence
      OR NEW.relative_path IS NOT OLD.relative_path
      OR NEW.file_sha256 IS NOT OLD.file_sha256
      OR NEW.size_bytes IS NOT OLD.size_bytes
      OR NEW.dimension IS NOT OLD.dimension
      OR NEW.metric IS NOT OLD.metric
      OR NEW.ntotal IS NOT OLD.ntotal
      OR NEW.created_at IS NOT OLD.created_at
      OR (NEW.state IS OLD.state AND NEW.state_changed_at IS NOT OLD.state_changed_at)
    BEGIN
        SELECT RAISE(ABORT, 'delta segment definition is immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_segments_state_transition
    BEFORE UPDATE OF state ON retrieval_delta_segments
    WHEN NEW.state <> OLD.state
      AND NOT (
          (OLD.state = 'staged' AND NEW.state IN ('ready', 'failed'))
          OR (
              OLD.state IN ('ready', 'failed')
              AND NEW.state = 'compacted'
              AND NOT EXISTS (
                  SELECT 1
                  FROM retrieval_runtime AS runtime
                  WHERE runtime.runtime_id = 1
                    AND runtime.active_snapshot_id = OLD.base_snapshot_id
                    AND runtime.publication_generation =
                        OLD.base_publication_generation
              )
          )
      )
    BEGIN
        SELECT RAISE(ABORT, 'illegal delta segment state transition');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_reports_validate_insert
    BEFORE INSERT ON retrieval_delta_reports
    WHEN NOT EXISTS (
        SELECT 1
        FROM retrieval_delta_segments AS segment
        LEFT JOIN reports AS report ON report.report_uid = NEW.report_uid
        WHERE segment.segment_id = NEW.segment_id
          AND segment.state = 'staged'
          AND (
              NEW.action = 'delete'
              OR report.canonical_relative_path = NEW.canonical_relative_path
          )
    )
    BEGIN
        SELECT RAISE(ABORT, 'delta report must match a staged segment and source path');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_reports_no_update
    BEFORE UPDATE ON retrieval_delta_reports
    BEGIN
        SELECT RAISE(ABORT, 'delta report actions are immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_reports_no_delete
    BEFORE DELETE ON retrieval_delta_reports
    BEGIN
        SELECT RAISE(ABORT, 'delta report actions are durable audit records');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_membership_validate_insert
    BEFORE INSERT ON retrieval_delta_membership
    WHEN NOT EXISTS (
        SELECT 1
        FROM retrieval_delta_segments AS segment
        JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = NEW.chunk_uid
        JOIN retrieval_parents AS parent
          ON parent.parent_uid = chunk.parent_uid
         AND parent.profile_id = chunk.profile_id
        JOIN reports AS report ON report.report_id = parent.report_id
        JOIN retrieval_delta_reports AS action
          ON action.segment_id = segment.segment_id
         AND action.report_uid = report.report_uid
         AND action.action = 'upsert'
        JOIN vector_snapshots AS snapshot
          ON snapshot.snapshot_id = segment.base_snapshot_id
        JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
        WHERE segment.segment_id = NEW.segment_id
          AND segment.state = 'staged'
          AND NEW.faiss_id <= segment.ntotal
          AND chunk.profile_id = build.profile_id
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'delta membership must match a staged upsert, profile, and ID range'
        );
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_membership_no_update
    BEFORE UPDATE ON retrieval_delta_membership
    BEGIN
        SELECT RAISE(ABORT, 'delta membership is immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_membership_no_delete
    BEFORE DELETE ON retrieval_delta_membership
    BEGIN
        SELECT RAISE(ABORT, 'delta membership is durable audit data');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_segments_validate_ready
    BEFORE UPDATE OF state ON retrieval_delta_segments
    WHEN NEW.state = 'ready' AND OLD.state <> 'ready'
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM retrieval_runtime AS runtime
            WHERE runtime.runtime_id = 1
              AND runtime.active_snapshot_id = OLD.base_snapshot_id
              AND runtime.publication_generation =
                  OLD.base_publication_generation
        )
        THEN RAISE(ABORT, 'delta segment base is not the active runtime') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM vector_snapshots AS snapshot
            WHERE snapshot.snapshot_id = OLD.base_snapshot_id
              AND snapshot.dimension = OLD.dimension
              AND snapshot.metric = OLD.metric
        )
        THEN RAISE(ABORT, 'delta descriptor does not match its base snapshot') END;
        SELECT CASE WHEN (
            SELECT count(*) FROM retrieval_delta_membership
            WHERE segment_id = OLD.segment_id
        ) <> OLD.ntotal
        THEN RAISE(ABORT, 'delta membership count does not match ntotal') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM retrieval_delta_reports
            WHERE segment_id = OLD.segment_id
        )
        THEN RAISE(ABORT, 'delta segment must contain at least one report action') END;
        SELECT CASE WHEN EXISTS (
            SELECT 1
            FROM retrieval_delta_reports AS action
            WHERE action.segment_id = OLD.segment_id
              AND action.action = 'upsert'
              AND NOT EXISTS (
                  SELECT 1
                  FROM retrieval_delta_membership AS membership
                  JOIN retrieval_chunks AS chunk
                    ON chunk.chunk_uid = membership.chunk_uid
                  JOIN retrieval_parents AS parent
                    ON parent.parent_uid = chunk.parent_uid
                   AND parent.profile_id = chunk.profile_id
                  JOIN reports AS report ON report.report_id = parent.report_id
                  WHERE membership.segment_id = action.segment_id
                    AND report.report_uid = action.report_uid
              )
        )
        THEN RAISE(ABORT, 'delta upsert has no vector membership') END;
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_segments_no_delete
    BEFORE DELETE ON retrieval_delta_segments
    BEGIN
        SELECT RAISE(ABORT, 'delta segments are durable audit records');
    END
    ''',
)


_GC_TRIGGER_DDL = (
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_artifact_gc_validate_insert
    BEFORE INSERT ON retrieval_delta_artifact_gc
    WHEN NOT EXISTS (
        SELECT 1
        FROM retrieval_delta_segments AS segment
        JOIN vector_snapshots AS base
          ON base.snapshot_id = segment.base_snapshot_id
        WHERE segment.segment_id = NEW.segment_id
          AND segment.state = 'compacted'
          AND segment.relative_path IS NOT NULL
          AND base.state = 'garbage_collected'
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'delta artifact GC requires a compacted segment and collected base'
        );
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_artifact_gc_no_update
    BEFORE UPDATE ON retrieval_delta_artifact_gc
    BEGIN
        SELECT RAISE(ABORT, 'delta artifact GC records are immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_delta_artifact_gc_no_delete
    BEFORE DELETE ON retrieval_delta_artifact_gc
    BEGIN
        SELECT RAISE(ABORT, 'delta artifact GC records are durable audit data');
    END
    ''',
)


_ACTIVE_REPORTS_VIEW_DDL = '''
CREATE VIEW active_reports AS
WITH ready_segments AS (
    SELECT segment.segment_id, segment.sequence
    FROM retrieval_delta_segments AS segment
    JOIN retrieval_runtime AS runtime
      ON runtime.runtime_id = 1
     AND runtime.active_snapshot_id = segment.base_snapshot_id
     AND runtime.publication_generation = segment.base_publication_generation
    WHERE segment.state = 'ready'
),
ranked_heads AS (
    SELECT action.canonical_relative_path, action.action, action.report_uid,
           action.segment_id, segment.sequence,
           row_number() OVER (
               PARTITION BY action.canonical_relative_path
               ORDER BY segment.sequence DESC, segment.segment_id DESC
           ) AS position
    FROM retrieval_delta_reports AS action
    JOIN ready_segments AS segment ON segment.segment_id = action.segment_id
    WHERE action.action IN ('upsert', 'delete')
),
heads AS (
    SELECT canonical_relative_path, action, report_uid, segment_id, sequence
    FROM ranked_heads
    WHERE position = 1
)
SELECT DISTINCT
    report.report_id,
    report.report_uid,
    report.canonical_relative_path,
    report.source_sha256,
    report.retrieval_metadata_sha256,
    report.report_type,
    report.report_date,
    report.target_name,
    report.title,
    report.broker,
    report.created_at
FROM retrieval_runtime AS runtime
JOIN retrieval_builds AS build
  ON build.build_id = runtime.active_build_id
 AND build.state = 'fully_complete'
JOIN vector_snapshots AS snapshot
  ON snapshot.snapshot_id = runtime.active_snapshot_id
 AND snapshot.build_id = build.build_id
 AND snapshot.state = 'ready'
JOIN snapshot_membership AS membership
  ON membership.snapshot_id = snapshot.snapshot_id
JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
JOIN retrieval_parents AS parent
  ON parent.parent_uid = chunk.parent_uid
 AND parent.profile_id = chunk.profile_id
JOIN reports AS report ON report.report_id = parent.report_id
LEFT JOIN heads AS head
  ON head.canonical_relative_path = report.canonical_relative_path
WHERE runtime.runtime_id = 1
  AND head.canonical_relative_path IS NULL
UNION ALL
SELECT
    report.report_id,
    report.report_uid,
    report.canonical_relative_path,
    report.source_sha256,
    report.retrieval_metadata_sha256,
    report.report_type,
    report.report_date,
    report.target_name,
    report.title,
    report.broker,
    report.created_at
FROM heads AS head
JOIN reports AS report ON report.report_uid = head.report_uid
WHERE head.action = 'upsert'
'''


_ACTIVE_VECTOR_MEMBERSHIP_VIEW_DDL = '''
CREATE VIEW active_vector_membership AS
WITH ready_segments AS (
    SELECT segment.segment_id, segment.sequence
    FROM retrieval_delta_segments AS segment
    JOIN retrieval_runtime AS runtime
      ON runtime.runtime_id = 1
     AND runtime.active_snapshot_id = segment.base_snapshot_id
     AND runtime.publication_generation = segment.base_publication_generation
    WHERE segment.state = 'ready'
),
ranked_heads AS (
    SELECT action.canonical_relative_path, action.action, action.report_uid,
           action.segment_id, segment.sequence,
           row_number() OVER (
               PARTITION BY action.canonical_relative_path
               ORDER BY segment.sequence DESC, segment.segment_id DESC
           ) AS position
    FROM retrieval_delta_reports AS action
    JOIN ready_segments AS segment ON segment.segment_id = action.segment_id
    WHERE action.action IN ('upsert', 'delete')
),
heads AS (
    SELECT canonical_relative_path, action, report_uid, segment_id, sequence
    FROM ranked_heads
    WHERE position = 1
)
SELECT
    snapshot.snapshot_id AS artifact_id,
    'base' AS artifact_kind,
    0 AS sequence,
    membership.faiss_id,
    membership.chunk_uid
FROM retrieval_runtime AS runtime
JOIN retrieval_builds AS build
  ON build.build_id = runtime.active_build_id
 AND build.state = 'fully_complete'
JOIN vector_snapshots AS snapshot
  ON snapshot.snapshot_id = runtime.active_snapshot_id
 AND snapshot.build_id = build.build_id
 AND snapshot.state = 'ready'
JOIN snapshot_membership AS membership
  ON membership.snapshot_id = snapshot.snapshot_id
JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
JOIN retrieval_parents AS parent
  ON parent.parent_uid = chunk.parent_uid
 AND parent.profile_id = chunk.profile_id
JOIN reports AS report ON report.report_id = parent.report_id
LEFT JOIN heads AS head
  ON head.canonical_relative_path = report.canonical_relative_path
WHERE runtime.runtime_id = 1
  AND head.canonical_relative_path IS NULL
UNION ALL
SELECT
    head.segment_id AS artifact_id,
    'delta' AS artifact_kind,
    head.sequence,
    membership.faiss_id,
    membership.chunk_uid
FROM heads AS head
JOIN retrieval_delta_membership AS membership
  ON membership.segment_id = head.segment_id
JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
JOIN retrieval_parents AS parent
  ON parent.parent_uid = chunk.parent_uid
 AND parent.profile_id = chunk.profile_id
JOIN reports AS report
  ON report.report_id = parent.report_id
 AND report.report_uid = head.report_uid
WHERE head.action = 'upsert'
'''


_EXPECTED_COLUMNS = {
    'retrieval_delta_segments': (
        'segment_id',
        'base_snapshot_id',
        'base_publication_generation',
        'sequence',
        'relative_path',
        'file_sha256',
        'size_bytes',
        'dimension',
        'metric',
        'ntotal',
        'state',
        'created_at',
        'state_changed_at',
    ),
    'retrieval_delta_reports': (
        'segment_id',
        'canonical_relative_path',
        'action',
        'report_uid',
        'reason_code',
    ),
    'retrieval_delta_membership': ('segment_id', 'chunk_uid', 'faiss_id'),
}

_EXPECTED_GC_COLUMNS = {
    'retrieval_delta_artifact_gc': ('segment_id', 'collected_at'),
}

_EXPECTED_VIEW_COLUMNS = {
    'active_reports': (
        'report_id',
        'report_uid',
        'canonical_relative_path',
        'source_sha256',
        'retrieval_metadata_sha256',
        'report_type',
        'report_date',
        'target_name',
        'title',
        'broker',
        'created_at',
    ),
    'active_vector_membership': (
        'artifact_id',
        'artifact_kind',
        'sequence',
        'faiss_id',
        'chunk_uid',
    ),
}


def delta_schema_installed(connection: sqlite3.Connection) -> bool:
    '''Return whether the complete extension is installed, rejecting partial state.'''

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }
    present = tables.intersection(DELTA_TABLES)
    present_gc = tables.intersection(DELTA_GC_TABLES)
    if not present:
        if present_gc:
            raise SchemaError(
                'retrieval delta GC schema exists without its core extension'
            )
        return False
    missing = DELTA_TABLES.difference(present)
    if missing:
        raise SchemaError(
            'retrieval delta schema is only partially installed: '
            + ', '.join(sorted(missing))
        )
    _validate_delta_columns(connection)
    views = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'view'"
        )
    }
    missing_views = DELTA_VIEWS.difference(views)
    if missing_views:
        raise SchemaError(
            'retrieval delta schema is only partially installed: '
            + ', '.join(sorted(missing_views))
        )
    indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'index'"
        )
    }
    missing_indexes = DELTA_INDEXES.difference(indexes)
    if missing_indexes:
        raise SchemaError(
            'retrieval delta schema is missing required indexes: '
            + ', '.join(sorted(missing_indexes))
        )
    triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
        )
    }
    missing_triggers = DELTA_TRIGGERS.difference(triggers)
    if missing_triggers:
        raise SchemaError(
            'retrieval delta schema is missing required triggers: '
            + ', '.join(sorted(missing_triggers))
        )
    _delta_gc_schema_installed(connection, tables=tables, triggers=triggers)
    active_reports_sql = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'view' AND name = 'active_reports'"
    ).fetchone()
    if (
        active_reports_sql is None
        or 'retrieval_delta_reports' not in str(active_reports_sql[0]).lower()
    ):
        raise SchemaError('active_reports view is not delta-aware')
    _validate_delta_views(connection)
    return True


def install_delta_schema(connection: sqlite3.Connection) -> None:
    '''Install the backward-compatible delta extension in one savepoint.'''

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('connection must be a sqlite3.Connection')
    configure_catalog_storage(connection, writable=True)
    if connection.in_transaction:
        raise SchemaError('delta schema must be installed outside a transaction')
    connection.execute('PRAGMA foreign_keys = ON')
    core_installed = delta_schema_installed(connection)
    if core_installed and _delta_gc_schema_installed(connection):
        return
    savepoint = 'install_retrieval_delta_schema'
    connection.execute(f'SAVEPOINT {savepoint}')
    try:
        if not core_installed:
            _execute_all(connection, _TABLE_DDL)
            _execute_all(connection, _INDEX_DDL)
            _execute_all(connection, _TRIGGER_DDL)
            connection.execute('DROP VIEW IF EXISTS active_vector_membership')
            connection.execute('DROP VIEW IF EXISTS active_reports')
            connection.execute(_ACTIVE_REPORTS_VIEW_DDL)
            connection.execute(_ACTIVE_VECTOR_MEMBERSHIP_VIEW_DDL)
        _execute_all(connection, _GC_TABLE_DDL)
        _execute_all(connection, _GC_TRIGGER_DDL)
        _validate_delta_schema(connection)
        if not _delta_gc_schema_installed(connection):
            raise SchemaError('retrieval delta artifact GC schema is missing')
    except Exception:
        connection.execute(f'ROLLBACK TO {savepoint}')
        connection.execute(f'RELEASE {savepoint}')
        raise
    else:
        connection.execute(f'RELEASE {savepoint}')


def _execute_all(connection: sqlite3.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        connection.execute(statement)


def _validate_delta_schema(connection: sqlite3.Connection) -> None:
    if not delta_schema_installed(connection):
        raise SchemaError('retrieval delta schema is missing')


def _validate_delta_columns(connection: sqlite3.Connection) -> None:
    for table, expected in _EXPECTED_COLUMNS.items():
        actual = tuple(
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            raise SchemaError(
                f'incompatible {table} columns: expected {expected}, got {actual}'
            )


def _delta_gc_schema_installed(
    connection: sqlite3.Connection,
    *,
    tables: set[str] | None = None,
    triggers: set[str] | None = None,
) -> bool:
    known_tables = tables or {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }
    present = known_tables.intersection(DELTA_GC_TABLES)
    if not present:
        return False
    missing = DELTA_GC_TABLES.difference(present)
    if missing:
        raise SchemaError(
            'retrieval delta artifact GC schema is only partially installed: '
            + ', '.join(sorted(missing))
        )
    for table, expected in _EXPECTED_GC_COLUMNS.items():
        actual = tuple(
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            raise SchemaError(
                f'incompatible {table} columns: expected {expected}, got {actual}'
            )
    known_triggers = triggers or {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
        )
    }
    missing_triggers = DELTA_GC_TRIGGERS.difference(known_triggers)
    if missing_triggers:
        raise SchemaError(
            'retrieval delta artifact GC schema is missing required triggers: '
            + ', '.join(sorted(missing_triggers))
        )
    return True


def _validate_delta_views(connection: sqlite3.Connection) -> None:
    for view, expected in _EXPECTED_VIEW_COLUMNS.items():
        actual = tuple(
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{view}")')
        )
        if actual != expected:
            raise SchemaError(
                f'incompatible {view} columns: expected {expected}, got {actual}'
            )


__all__ = [
    'DELTA_GC_TABLES',
    'DELTA_GC_TRIGGERS',
    'DELTA_INDEXES',
    'DELTA_TABLES',
    'DELTA_TRIGGERS',
    'DELTA_VIEWS',
    'delta_schema_installed',
    'install_delta_schema',
]
