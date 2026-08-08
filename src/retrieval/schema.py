'''Native SQLite catalog schema for V2 retrieval.

SQLite owns logical identity, immutable content, snapshot membership, and the
runtime control plane.  FAISS artifacts referenced by this schema contain only
vectors and snapshot-local positive integer IDs.
'''

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path


SCHEMA_VERSION = 3
MAX_SIGNED_INT64 = (1 << 63) - 1

RETRIEVAL_TABLES = frozenset(
    {
        'reports',
        'retrieval_parents',
        'retrieval_chunks',
        'embedding_profiles',
        'retrieval_builds',
        'vector_snapshots',
        'snapshot_membership',
        'retrieval_runtime',
        'publication_runs',
    }
)


class SchemaError(RuntimeError):
    '''Raised when a database cannot safely satisfy the native V2 contract.'''


_TABLE_DDL = (
    '''
    CREATE TABLE IF NOT EXISTS reports (
        report_id INTEGER PRIMARY KEY CHECK (report_id > 0),
        report_uid TEXT NOT NULL
            CHECK (
                length(report_uid) = 64
                AND report_uid = lower(report_uid)
                AND report_uid NOT GLOB '*[^0-9a-f]*'
            ),
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
        source_sha256 TEXT NOT NULL
            CHECK (
                length(source_sha256) = 64
                AND source_sha256 = lower(source_sha256)
                AND source_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        retrieval_metadata_sha256 TEXT NOT NULL
            CHECK (
                length(retrieval_metadata_sha256) = 64
                AND retrieval_metadata_sha256 = lower(retrieval_metadata_sha256)
                AND retrieval_metadata_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        report_type TEXT NOT NULL CHECK (length(trim(report_type)) > 0),
        report_date TEXT NOT NULL
            CHECK (
                length(report_date) = 10
                AND report_date GLOB
                    '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
            ),
        target_name TEXT CHECK (
            target_name IS NULL OR length(trim(target_name)) > 0
        ),
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        broker TEXT NOT NULL CHECK (length(trim(broker)) > 0),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        UNIQUE (report_uid),
        UNIQUE (
            canonical_relative_path,
            source_sha256,
            retrieval_metadata_sha256
        )
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS embedding_profiles (
        profile_id TEXT PRIMARY KEY CHECK (length(trim(profile_id)) > 0),
        profile_hash TEXT NOT NULL
            CHECK (
                length(profile_hash) = 64
                AND profile_hash = lower(profile_hash)
                AND profile_hash NOT GLOB '*[^0-9a-f]*'
            ),
        model TEXT NOT NULL CHECK (length(trim(model)) > 0),
        dimension INTEGER NOT NULL CHECK (dimension > 0),
        metric TEXT NOT NULL CHECK (metric IN ('l2', 'inner_product')),
        normalization INTEGER NOT NULL CHECK (normalization IN (0, 1)),
        prefix_template TEXT NOT NULL,
        extractor TEXT NOT NULL CHECK (length(trim(extractor)) > 0),
        parent_policy_json TEXT NOT NULL
            CHECK (
                json_valid(parent_policy_json)
                AND json_type(parent_policy_json) = 'object'
            ),
        child_policy_json TEXT NOT NULL
            CHECK (
                json_valid(child_policy_json)
                AND json_type(child_policy_json) = 'object'
            ),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        UNIQUE (profile_hash)
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS retrieval_parents (
        parent_uid TEXT PRIMARY KEY
            CHECK (
                length(parent_uid) = 64
                AND parent_uid = lower(parent_uid)
                AND parent_uid NOT GLOB '*[^0-9a-f]*'
            ),
        report_id INTEGER NOT NULL,
        profile_id TEXT NOT NULL,
        parent_order INTEGER NOT NULL CHECK (parent_order >= 0),
        content TEXT NOT NULL CHECK (length(content) > 0),
        content_sha256 TEXT NOT NULL
            CHECK (
                length(content_sha256) = 64
                AND content_sha256 = lower(content_sha256)
                AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        UNIQUE (profile_id, report_id, parent_order),
        UNIQUE (parent_uid, profile_id),
        FOREIGN KEY (report_id) REFERENCES reports(report_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (profile_id) REFERENCES embedding_profiles(profile_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS retrieval_chunks (
        chunk_uid TEXT PRIMARY KEY
            CHECK (
                length(chunk_uid) = 64
                AND chunk_uid = lower(chunk_uid)
                AND chunk_uid NOT GLOB '*[^0-9a-f]*'
            ),
        parent_uid TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        child_order INTEGER NOT NULL CHECK (child_order >= 0),
        span_start INTEGER NOT NULL CHECK (span_start >= 0),
        span_end INTEGER NOT NULL CHECK (span_end > span_start),
        embedding_text_sha256 TEXT NOT NULL
            CHECK (
                length(embedding_text_sha256) = 64
                AND embedding_text_sha256 = lower(embedding_text_sha256)
                AND embedding_text_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        UNIQUE (profile_id, parent_uid, child_order),
        FOREIGN KEY (parent_uid, profile_id)
            REFERENCES retrieval_parents(parent_uid, profile_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS retrieval_builds (
        build_id TEXT PRIMARY KEY CHECK (length(trim(build_id)) > 0),
        profile_id TEXT NOT NULL,
        source_manifest_json TEXT NOT NULL
            CHECK (
                json_valid(source_manifest_json)
                AND json_type(source_manifest_json) = 'object'
            ),
        source_manifest_sha256 TEXT NOT NULL
            CHECK (
                length(source_manifest_sha256) = 64
                AND source_manifest_sha256 = lower(source_manifest_sha256)
                AND source_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        included_count INTEGER NOT NULL CHECK (included_count >= 0),
        excluded_count INTEGER NOT NULL CHECK (excluded_count >= 0),
        expected_count INTEGER NOT NULL CHECK (expected_count >= 0),
        exclusion_policy_version TEXT NOT NULL
            CHECK (length(trim(exclusion_policy_version)) > 0),
        state TEXT NOT NULL DEFAULT 'planned'
            CHECK (
                state IN (
                    'planned',
                    'cataloging',
                    'vector_building',
                    'validating',
                    'ready',
                    'committed_pending_checkpoint',
                    'fully_complete',
                    'failed'
                )
            ),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        state_changed_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(state_changed_at) > 0),
        CHECK (included_count + excluded_count = expected_count),
        FOREIGN KEY (profile_id) REFERENCES embedding_profiles(profile_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    ''',
    f'''
    CREATE TABLE IF NOT EXISTS vector_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (length(trim(snapshot_id)) > 0),
        build_id TEXT NOT NULL,
        relative_path TEXT NOT NULL
            CHECK (
                length(relative_path) > 0
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
                AND relative_path NOT LIKE '../%'
                AND relative_path NOT LIKE '%/../%'
                AND relative_path NOT LIKE '%/..'
            ),
        file_sha256 TEXT NOT NULL
            CHECK (
                length(file_sha256) = 64
                AND file_sha256 = lower(file_sha256)
                AND file_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
        dimension INTEGER NOT NULL CHECK (dimension > 0),
        metric TEXT NOT NULL CHECK (metric IN ('l2', 'inner_product')),
        ntotal INTEGER NOT NULL
            CHECK (ntotal >= 0 AND ntotal <= {MAX_SIGNED_INT64}),
        state TEXT NOT NULL DEFAULT 'staged'
            CHECK (
                state IN (
                    'staged',
                    'validating',
                    'ready',
                    'failed',
                    'garbage_pending',
                    'garbage_collected'
                )
            ),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        state_changed_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(state_changed_at) > 0),
        UNIQUE (relative_path),
        UNIQUE (file_sha256),
        FOREIGN KEY (build_id) REFERENCES retrieval_builds(build_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    ''',
    f'''
    CREATE TABLE IF NOT EXISTS snapshot_membership (
        snapshot_id TEXT NOT NULL,
        chunk_uid TEXT NOT NULL,
        faiss_id INTEGER NOT NULL
            CHECK (faiss_id > 0 AND faiss_id <= {MAX_SIGNED_INT64}),
        PRIMARY KEY (snapshot_id, chunk_uid),
        UNIQUE (snapshot_id, faiss_id),
        FOREIGN KEY (snapshot_id) REFERENCES vector_snapshots(snapshot_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (chunk_uid) REFERENCES retrieval_chunks(chunk_uid)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    ) WITHOUT ROWID
    ''',
    f'''
    CREATE TABLE IF NOT EXISTS retrieval_runtime (
        runtime_id INTEGER PRIMARY KEY DEFAULT 1 CHECK (runtime_id = 1),
        schema_version INTEGER NOT NULL DEFAULT {SCHEMA_VERSION}
            CHECK (schema_version >= {SCHEMA_VERSION}),
        active_snapshot_id TEXT,
        active_build_id TEXT,
        predecessor_snapshot_id TEXT,
        publication_generation INTEGER NOT NULL DEFAULT 0
            CHECK (publication_generation >= 0),
        write_epoch INTEGER NOT NULL DEFAULT 0 CHECK (write_epoch >= 0),
        degraded INTEGER NOT NULL DEFAULT 0 CHECK (degraded IN (0, 1)),
        write_enabled INTEGER NOT NULL DEFAULT 0
            CHECK (write_enabled IN (0, 1)),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        updated_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(updated_at) > 0),
        CHECK (
            (active_snapshot_id IS NULL AND active_build_id IS NULL)
            OR
            (active_snapshot_id IS NOT NULL AND active_build_id IS NOT NULL)
        ),
        CHECK (
            predecessor_snapshot_id IS NULL OR active_snapshot_id IS NOT NULL
        ),
        CHECK (
            predecessor_snapshot_id IS NULL
            OR predecessor_snapshot_id <> active_snapshot_id
        ),
        CHECK (degraded = 0 OR write_enabled = 0),
        CHECK (write_enabled = 0 OR active_snapshot_id IS NOT NULL),
        FOREIGN KEY (active_snapshot_id)
            REFERENCES vector_snapshots(snapshot_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (active_build_id) REFERENCES retrieval_builds(build_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (predecessor_snapshot_id)
            REFERENCES vector_snapshots(snapshot_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS publication_runs (
        publication_id TEXT PRIMARY KEY
            CHECK (length(trim(publication_id)) > 0),
        from_snapshot_id TEXT,
        to_snapshot_id TEXT,
        phase TEXT NOT NULL DEFAULT 'journal_created'
            CHECK (
                phase IN (
                    'journal_created',
                    'catalog_written',
                    'artifact_written',
                    'artifact_durable',
                    'artifact_published',
                    'artifact_validated',
                    'rollback_backup_validated',
                    'commit_intent_durable',
                    'committed_pending_checkpoint',
                    'checkpoint_validated',
                    'committed_floor_durable',
                    'fully_complete'
                )
            ),
        state TEXT NOT NULL DEFAULT 'running'
            CHECK (state IN ('running', 'failed', 'fully_complete')),
        evidence_manifest_relative_path TEXT,
        evidence_manifest_sha256 TEXT,
        error_code TEXT CHECK (
            error_code IS NULL OR length(trim(error_code)) > 0
        ),
        created_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(created_at) > 0),
        updated_at TEXT NOT NULL DEFAULT (
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        ) CHECK (length(updated_at) > 0),
        CHECK (
            (evidence_manifest_relative_path IS NULL
             AND evidence_manifest_sha256 IS NULL)
            OR
            (
                evidence_manifest_relative_path IS NOT NULL
                AND evidence_manifest_sha256 IS NOT NULL
                AND length(evidence_manifest_relative_path) > 0
                AND evidence_manifest_relative_path NOT LIKE '/%'
                AND evidence_manifest_relative_path NOT GLOB '[A-Za-z]:*'
                AND instr(evidence_manifest_relative_path, char(0)) = 0
                AND instr(evidence_manifest_relative_path, '\\') = 0
                AND evidence_manifest_relative_path <> '..'
                AND evidence_manifest_relative_path NOT LIKE '../%'
                AND evidence_manifest_relative_path NOT LIKE '%/../%'
                AND evidence_manifest_relative_path NOT LIKE '%/..'
                AND length(evidence_manifest_sha256) = 64
                AND evidence_manifest_sha256 = lower(evidence_manifest_sha256)
                AND evidence_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        CHECK (state <> 'failed' OR error_code IS NOT NULL),
        CHECK (
            state <> 'fully_complete'
            OR (
                phase = 'fully_complete'
                AND to_snapshot_id IS NOT NULL
                AND evidence_manifest_sha256 IS NOT NULL
            )
        ),
        FOREIGN KEY (from_snapshot_id)
            REFERENCES vector_snapshots(snapshot_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY (to_snapshot_id)
            REFERENCES vector_snapshots(snapshot_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )
    ''',
)


_INDEX_DDL = (
    '''CREATE INDEX IF NOT EXISTS idx_reports_canonical_relative_path
       ON reports(canonical_relative_path)''',
    '''CREATE INDEX IF NOT EXISTS idx_reports_filter_scope
       ON reports(report_type, report_date, target_name)''',
    '''CREATE INDEX IF NOT EXISTS idx_retrieval_parents_report
       ON retrieval_parents(report_id, profile_id, parent_order)''',
    '''CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_parent
       ON retrieval_chunks(parent_uid, profile_id, child_order)''',
    '''CREATE INDEX IF NOT EXISTS idx_retrieval_builds_profile_state
       ON retrieval_builds(profile_id, state)''',
    '''CREATE INDEX IF NOT EXISTS idx_vector_snapshots_build_state
       ON vector_snapshots(build_id, state)''',
    '''CREATE INDEX IF NOT EXISTS idx_snapshot_membership_chunk
       ON snapshot_membership(chunk_uid)''',
    '''CREATE INDEX IF NOT EXISTS idx_publication_runs_state_phase
       ON publication_runs(state, phase)''',
)


_TRIGGER_DDL = (
    '''
    CREATE TRIGGER IF NOT EXISTS reports_no_update
    BEFORE UPDATE ON reports
    BEGIN
        SELECT RAISE(ABORT, 'reports are immutable source objects');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS reports_no_delete
    BEFORE DELETE ON reports
    BEGIN
        SELECT RAISE(ABORT, 'reports are append-only source objects');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS embedding_profiles_no_update
    BEFORE UPDATE ON embedding_profiles
    BEGIN
        SELECT RAISE(ABORT, 'embedding profiles are immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS embedding_profiles_no_delete
    BEFORE DELETE ON embedding_profiles
    BEGIN
        SELECT RAISE(ABORT, 'embedding profiles are append-only');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_parents_no_update
    BEFORE UPDATE ON retrieval_parents
    BEGIN
        SELECT RAISE(ABORT, 'retrieval parents are immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_parents_no_delete
    BEFORE DELETE ON retrieval_parents
    BEGIN
        SELECT RAISE(ABORT, 'retrieval parents are append-only');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_chunks_validate_span_insert
    BEFORE INSERT ON retrieval_chunks
    WHEN NOT EXISTS (
        SELECT 1
        FROM retrieval_parents AS parent
        WHERE parent.parent_uid = NEW.parent_uid
          AND parent.profile_id = NEW.profile_id
          AND NEW.span_end <= length(parent.content)
    )
    BEGIN
        SELECT RAISE(ABORT, 'chunk span exceeds its canonical parent');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_chunks_no_update
    BEFORE UPDATE ON retrieval_chunks
    BEGIN
        SELECT RAISE(ABORT, 'retrieval chunks are immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_chunks_no_delete
    BEFORE DELETE ON retrieval_chunks
    BEGIN
        SELECT RAISE(ABORT, 'retrieval chunks are append-only');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_builds_require_initial_state
    BEFORE INSERT ON retrieval_builds
    WHEN NEW.state <> 'planned'
    BEGIN
        SELECT RAISE(ABORT, 'retrieval build must start in planned state');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_builds_immutable_fields
    BEFORE UPDATE ON retrieval_builds
    WHEN NEW.build_id IS NOT OLD.build_id
      OR NEW.profile_id IS NOT OLD.profile_id
      OR NEW.source_manifest_json IS NOT OLD.source_manifest_json
      OR NEW.source_manifest_sha256 IS NOT OLD.source_manifest_sha256
      OR NEW.included_count IS NOT OLD.included_count
      OR NEW.excluded_count IS NOT OLD.excluded_count
      OR NEW.expected_count IS NOT OLD.expected_count
      OR NEW.exclusion_policy_version IS NOT OLD.exclusion_policy_version
      OR NEW.created_at IS NOT OLD.created_at
      OR (
          NEW.state IS OLD.state
          AND NEW.state_changed_at IS NOT OLD.state_changed_at
      )
    BEGIN
        SELECT RAISE(ABORT, 'retrieval build definition is immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_builds_state_transition
    BEFORE UPDATE OF state ON retrieval_builds
    WHEN NEW.state <> OLD.state
      AND NOT (
          (OLD.state = 'planned'
           AND NEW.state IN ('cataloging', 'failed'))
          OR (OLD.state = 'cataloging'
              AND NEW.state IN ('vector_building', 'failed'))
          OR (OLD.state = 'vector_building'
              AND NEW.state IN ('validating', 'failed'))
          OR (OLD.state = 'validating'
              AND NEW.state IN ('ready', 'failed'))
          OR (OLD.state = 'ready'
              AND NEW.state IN ('committed_pending_checkpoint', 'failed'))
          OR (OLD.state = 'committed_pending_checkpoint'
              AND NEW.state = 'fully_complete')
      )
    BEGIN
        SELECT RAISE(ABORT, 'illegal retrieval build state transition');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_builds_no_delete
    BEFORE DELETE ON retrieval_builds
    BEGIN
        SELECT RAISE(ABORT, 'retrieval builds are durable audit records');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS vector_snapshots_require_initial_state
    BEFORE INSERT ON vector_snapshots
    WHEN NEW.state <> 'staged'
    BEGIN
        SELECT RAISE(ABORT, 'vector snapshot must start in staged state');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS vector_snapshots_validate_profile_insert
    BEFORE INSERT ON vector_snapshots
    WHEN NOT EXISTS (
        SELECT 1
        FROM retrieval_builds AS build
        JOIN embedding_profiles AS profile
          ON profile.profile_id = build.profile_id
        WHERE build.build_id = NEW.build_id
          AND profile.dimension = NEW.dimension
          AND profile.metric = NEW.metric
    )
    BEGIN
        SELECT RAISE(ABORT, 'snapshot does not match its embedding profile');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS vector_snapshots_immutable_fields
    BEFORE UPDATE ON vector_snapshots
    WHEN NEW.snapshot_id IS NOT OLD.snapshot_id
      OR NEW.build_id IS NOT OLD.build_id
      OR NEW.relative_path IS NOT OLD.relative_path
      OR NEW.file_sha256 IS NOT OLD.file_sha256
      OR NEW.size_bytes IS NOT OLD.size_bytes
      OR NEW.dimension IS NOT OLD.dimension
      OR NEW.metric IS NOT OLD.metric
      OR NEW.ntotal IS NOT OLD.ntotal
      OR NEW.created_at IS NOT OLD.created_at
      OR (
          NEW.state IS OLD.state
          AND NEW.state_changed_at IS NOT OLD.state_changed_at
      )
    BEGIN
        SELECT RAISE(ABORT, 'vector snapshot descriptor is immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS vector_snapshots_state_transition
    BEFORE UPDATE OF state ON vector_snapshots
    WHEN NEW.state <> OLD.state
      AND NOT (
          (OLD.state = 'staged'
           AND NEW.state IN ('validating', 'failed'))
          OR (OLD.state = 'validating'
              AND NEW.state IN ('ready', 'failed'))
          OR (OLD.state = 'ready'
              AND NEW.state IN ('failed', 'garbage_pending'))
          OR (OLD.state = 'failed'
              AND NEW.state = 'garbage_pending')
          OR (OLD.state = 'garbage_pending'
              AND NEW.state = 'garbage_collected')
      )
    BEGIN
        SELECT RAISE(ABORT, 'illegal vector snapshot state transition');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS vector_snapshots_validate_ready
    BEFORE UPDATE OF state ON vector_snapshots
    WHEN NEW.state = 'ready' AND OLD.state <> 'ready'
    BEGIN
        SELECT CASE
            WHEN (
                SELECT count(*)
                FROM snapshot_membership
                WHERE snapshot_id = OLD.snapshot_id
            ) <> OLD.ntotal
            THEN RAISE(ABORT, 'snapshot membership count does not match ntotal')
        END;
        SELECT CASE
            WHEN NOT EXISTS (
                SELECT 1
                FROM retrieval_builds
                WHERE build_id = OLD.build_id
                  AND state IN (
                      'validating',
                      'ready',
                      'committed_pending_checkpoint',
                      'fully_complete'
                  )
            )
            THEN RAISE(ABORT, 'snapshot build has not reached validation')
        END;
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS vector_snapshots_no_delete
    BEFORE DELETE ON vector_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'vector snapshots are immutable audit records');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS snapshot_membership_validate_insert
    BEFORE INSERT ON snapshot_membership
    WHEN NOT EXISTS (
        SELECT 1
        FROM vector_snapshots AS snapshot
        JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
        JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = NEW.chunk_uid
        WHERE snapshot.snapshot_id = NEW.snapshot_id
          AND snapshot.state IN ('staged', 'validating')
          AND NEW.faiss_id <= snapshot.ntotal
          AND chunk.profile_id = build.profile_id
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'membership must match a mutable snapshot, profile, and ID range'
        );
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS snapshot_membership_no_update
    BEFORE UPDATE ON snapshot_membership
    BEGIN
        SELECT RAISE(ABORT, 'snapshot membership is immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS snapshot_membership_no_delete
    BEFORE DELETE ON snapshot_membership
    WHEN (
        SELECT state FROM vector_snapshots
        WHERE snapshot_id = OLD.snapshot_id
    ) NOT IN ('staged', 'validating', 'failed')
    BEGIN
        SELECT RAISE(ABORT, 'ready snapshot membership is immutable');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_runtime_validate_insert
    BEFORE INSERT ON retrieval_runtime
    WHEN NEW.active_snapshot_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM vector_snapshots AS snapshot
          JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
          WHERE snapshot.snapshot_id = NEW.active_snapshot_id
            AND snapshot.build_id = NEW.active_build_id
            AND snapshot.state = 'ready'
            AND build.state IN (
                'committed_pending_checkpoint',
                'fully_complete'
            )
      )
    BEGIN
        SELECT RAISE(ABORT, 'active runtime target is not a validated V2 build');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_runtime_monotonic_update
    BEFORE UPDATE ON retrieval_runtime
    WHEN NEW.runtime_id <> OLD.runtime_id
      OR NEW.schema_version < OLD.schema_version
      OR NEW.publication_generation < OLD.publication_generation
      OR NEW.write_epoch < OLD.write_epoch
      OR NEW.created_at IS NOT OLD.created_at
    BEGIN
        SELECT RAISE(ABORT, 'runtime generation and epoch are monotonic');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_runtime_validate_active_update
    BEFORE UPDATE ON retrieval_runtime
    WHEN NEW.active_snapshot_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM vector_snapshots AS snapshot
          JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
          WHERE snapshot.snapshot_id = NEW.active_snapshot_id
            AND snapshot.build_id = NEW.active_build_id
            AND snapshot.state = 'ready'
            AND build.state IN (
                'committed_pending_checkpoint',
                'fully_complete'
            )
      )
    BEGIN
        SELECT RAISE(ABORT, 'active runtime target is not a validated V2 build');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_runtime_validate_predecessor_update
    BEFORE UPDATE ON retrieval_runtime
    WHEN NEW.predecessor_snapshot_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM vector_snapshots AS snapshot
          JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
          WHERE snapshot.snapshot_id = NEW.predecessor_snapshot_id
            AND snapshot.state = 'ready'
            AND build.state = 'fully_complete'
      )
    BEGIN
        SELECT RAISE(ABORT, 'predecessor must be a verified raw V2 snapshot');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_runtime_validate_writable_update
    BEFORE UPDATE ON retrieval_runtime
    WHEN NEW.write_enabled = 1
      AND NOT EXISTS (
          SELECT 1
          FROM vector_snapshots AS snapshot
          JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
          WHERE snapshot.snapshot_id = NEW.active_snapshot_id
            AND snapshot.build_id = NEW.active_build_id
            AND snapshot.state = 'ready'
            AND build.state = 'fully_complete'
      )
    BEGIN
        SELECT RAISE(ABORT, 'writes require a fully complete active V2 build');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS retrieval_runtime_no_delete
    BEFORE DELETE ON retrieval_runtime
    BEGIN
        SELECT RAISE(ABORT, 'retrieval runtime singleton cannot be deleted');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS publication_runs_require_initial_state
    BEFORE INSERT ON publication_runs
    WHEN NEW.phase <> 'journal_created' OR NEW.state <> 'running'
    BEGIN
        SELECT RAISE(ABORT, 'publication journal must start at journal_created');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS publication_runs_immutable_fields
    BEFORE UPDATE ON publication_runs
    WHEN NEW.publication_id IS NOT OLD.publication_id
      OR NEW.from_snapshot_id IS NOT OLD.from_snapshot_id
      OR NEW.created_at IS NOT OLD.created_at
      OR (OLD.to_snapshot_id IS NOT NULL
          AND NEW.to_snapshot_id IS NOT OLD.to_snapshot_id)
      OR (OLD.evidence_manifest_relative_path IS NOT NULL
          AND NEW.evidence_manifest_relative_path
              IS NOT OLD.evidence_manifest_relative_path)
      OR (OLD.evidence_manifest_sha256 IS NOT NULL
          AND NEW.evidence_manifest_sha256
              IS NOT OLD.evidence_manifest_sha256)
      OR (OLD.error_code IS NOT NULL
          AND NEW.error_code IS NOT OLD.error_code)
    BEGIN
        SELECT RAISE(ABORT, 'publication identity and evidence are append-only');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS publication_runs_phase_monotonic
    BEFORE UPDATE OF phase ON publication_runs
    WHEN NEW.phase <> OLD.phase
      AND (
          CASE NEW.phase
              WHEN 'journal_created' THEN 0
              WHEN 'catalog_written' THEN 1
              WHEN 'artifact_written' THEN 2
              WHEN 'artifact_durable' THEN 3
              WHEN 'artifact_published' THEN 4
              WHEN 'artifact_validated' THEN 5
              WHEN 'rollback_backup_validated' THEN 6
              WHEN 'commit_intent_durable' THEN 7
              WHEN 'committed_pending_checkpoint' THEN 8
              WHEN 'checkpoint_validated' THEN 9
              WHEN 'committed_floor_durable' THEN 10
              WHEN 'fully_complete' THEN 11
          END
      ) < (
          CASE OLD.phase
              WHEN 'journal_created' THEN 0
              WHEN 'catalog_written' THEN 1
              WHEN 'artifact_written' THEN 2
              WHEN 'artifact_durable' THEN 3
              WHEN 'artifact_published' THEN 4
              WHEN 'artifact_validated' THEN 5
              WHEN 'rollback_backup_validated' THEN 6
              WHEN 'commit_intent_durable' THEN 7
              WHEN 'committed_pending_checkpoint' THEN 8
              WHEN 'checkpoint_validated' THEN 9
              WHEN 'committed_floor_durable' THEN 10
              WHEN 'fully_complete' THEN 11
          END
      )
    BEGIN
        SELECT RAISE(ABORT, 'publication phase cannot move backward');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS publication_runs_state_transition
    BEFORE UPDATE OF state ON publication_runs
    WHEN NEW.state <> OLD.state
      AND NOT (
          OLD.state = 'running'
          AND NEW.state IN ('failed', 'fully_complete')
      )
    BEGIN
        SELECT RAISE(ABORT, 'publication state is terminal and monotonic');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS publication_runs_no_post_commit_failure
    BEFORE UPDATE OF state ON publication_runs
    WHEN NEW.state = 'failed'
      AND OLD.phase IN (
          'committed_pending_checkpoint',
          'checkpoint_validated',
          'committed_floor_durable',
          'fully_complete'
      )
    BEGIN
        SELECT RAISE(ABORT, 'a committed publication cannot become failed');
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS publication_runs_no_delete
    BEFORE DELETE ON publication_runs
    BEGIN
        SELECT RAISE(ABORT, 'publication journals are durable audit records');
    END
    ''',
)


_VIEW_DDL = '''
CREATE VIEW IF NOT EXISTS active_reports AS
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
JOIN retrieval_chunks AS chunk
  ON chunk.chunk_uid = membership.chunk_uid
JOIN retrieval_parents AS parent
  ON parent.parent_uid = chunk.parent_uid
 AND parent.profile_id = chunk.profile_id
JOIN reports AS report
  ON report.report_id = parent.report_id
WHERE runtime.runtime_id = 1
'''


_EXPECTED_COLUMNS = {
    'reports': (
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
    'embedding_profiles': (
        'profile_id',
        'profile_hash',
        'model',
        'dimension',
        'metric',
        'normalization',
        'prefix_template',
        'extractor',
        'parent_policy_json',
        'child_policy_json',
        'created_at',
    ),
    'retrieval_parents': (
        'parent_uid',
        'report_id',
        'profile_id',
        'parent_order',
        'content',
        'content_sha256',
        'created_at',
    ),
    'retrieval_chunks': (
        'chunk_uid',
        'parent_uid',
        'profile_id',
        'child_order',
        'span_start',
        'span_end',
        'embedding_text_sha256',
        'created_at',
    ),
    'retrieval_builds': (
        'build_id',
        'profile_id',
        'source_manifest_json',
        'source_manifest_sha256',
        'included_count',
        'excluded_count',
        'expected_count',
        'exclusion_policy_version',
        'state',
        'created_at',
        'state_changed_at',
    ),
    'vector_snapshots': (
        'snapshot_id',
        'build_id',
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
    'snapshot_membership': ('snapshot_id', 'chunk_uid', 'faiss_id'),
    'retrieval_runtime': (
        'runtime_id',
        'schema_version',
        'active_snapshot_id',
        'active_build_id',
        'predecessor_snapshot_id',
        'publication_generation',
        'write_epoch',
        'degraded',
        'write_enabled',
        'created_at',
        'updated_at',
    ),
    'publication_runs': (
        'publication_id',
        'from_snapshot_id',
        'to_snapshot_id',
        'phase',
        'state',
        'evidence_manifest_relative_path',
        'evidence_manifest_sha256',
        'error_code',
        'created_at',
        'updated_at',
    ),
}


def install_schema(connection: sqlite3.Connection) -> None:
    '''Install and validate the native catalog schema atomically.

    Repeating this function against an installed catalog performs no schema or
    row changes.  An incompatible table fails closed instead of being
    silently treated as a native V2 table.
    '''
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('connection must be a sqlite3.Connection')

    has_objects = connection.execute(
        '''
        SELECT EXISTS (
            SELECT 1 FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
        )
        '''
    ).fetchone()[0]
    configure_catalog_storage(
        connection,
        initialize=not bool(has_objects),
        writable=True,
    )
    _enable_foreign_keys(connection)
    savepoint = 'install_native_retrieval_schema'
    connection.execute(f'SAVEPOINT {savepoint}')
    try:
        _execute_all(connection, _TABLE_DDL)
        _execute_all(connection, _INDEX_DDL)
        _execute_all(connection, _TRIGGER_DDL)
        connection.execute(_VIEW_DDL)
        connection.execute(
            '''
            INSERT OR IGNORE INTO retrieval_runtime (
                runtime_id,
                schema_version,
                publication_generation,
                write_epoch,
                degraded,
                write_enabled
            ) VALUES (1, ?, 0, 0, 0, 0)
            ''',
            (SCHEMA_VERSION,),
        )
        _validate_installed_schema(connection)
    except Exception:
        connection.execute(f'ROLLBACK TO {savepoint}')
        connection.execute(f'RELEASE {savepoint}')
        raise
    else:
        connection.execute(f'RELEASE {savepoint}')


def configure_catalog_storage(
    connection: sqlite3.Connection,
    *,
    initialize: bool = False,
    writable: bool = False,
) -> None:
    '''Initialize or validate the durable file-backed catalog journal contract.'''

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError('connection must be a sqlite3.Connection')
    main_path = next(
        (
            str(row[2])
            for row in connection.execute('PRAGMA database_list')
            if row[1] == 'main'
        ),
        '',
    )
    if not main_path:
        return
    if connection.in_transaction:
        raise SchemaError('catalog storage must be configured outside a transaction')
    pragma = 'PRAGMA journal_mode = WAL' if initialize else 'PRAGMA journal_mode'
    row = connection.execute(pragma).fetchone()
    mode = '' if row is None else str(row[0]).lower()
    if mode != 'wal':
        raise SchemaError('file-backed native catalog must use WAL journal mode')
    if writable:
        connection.execute('PRAGMA synchronous = FULL')
        synchronous = connection.execute('PRAGMA synchronous').fetchone()
        if synchronous is None or int(synchronous[0]) != 2:
            raise SchemaError('writable native catalog must use FULL synchronous mode')


def checkpoint_isolated_catalog(connection: sqlite3.Connection) -> None:
    '''Truncate WAL for a staging/backup database with no concurrent readers.'''

    configure_catalog_storage(connection, writable=True)
    row = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    if row is None or tuple(int(value) for value in row) != (0, 0, 0):
        raise SchemaError('isolated native catalog WAL checkpoint did not complete')


def require_main_file_only(path: str | Path) -> None:
    '''Reject committed SQLite sidecars before publishing an isolated copy.'''

    catalog = Path(path)
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = Path(f'{catalog}{suffix}')
        if not sidecar.exists():
            continue
        if sidecar.stat().st_size:
            raise SchemaError(
                f'isolated native catalog retains a nonempty {suffix} sidecar'
            )
        sidecar.unlink()


def _enable_foreign_keys(connection: sqlite3.Connection) -> None:
    enabled = int(connection.execute('PRAGMA foreign_keys').fetchone()[0])
    if enabled:
        return
    if connection.in_transaction:
        raise SchemaError(
            'foreign keys must be enabled before starting the install transaction'
        )
    connection.execute('PRAGMA foreign_keys = ON')
    enabled = int(connection.execute('PRAGMA foreign_keys').fetchone()[0])
    if not enabled:
        raise SchemaError('SQLite foreign-key enforcement is unavailable')


def _execute_all(
    connection: sqlite3.Connection,
    statements: Iterable[str],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _validate_installed_schema(connection: sqlite3.Connection) -> None:
    objects = {
        (row[0], row[1])
        for row in connection.execute(
            '''
            SELECT type, name
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            '''
        )
    }
    missing_tables = {
        table for table in RETRIEVAL_TABLES if ('table', table) not in objects
    }
    if missing_tables:
        raise SchemaError(
            'native retrieval tables are missing: '
            + ', '.join(sorted(missing_tables))
        )
    if ('view', 'active_reports') not in objects:
        raise SchemaError('active_reports view is missing')

    installed_tables = {name for kind, name in objects if kind == 'table'}
    unexpected_tables = installed_tables - RETRIEVAL_TABLES
    if unexpected_tables:
        raise SchemaError(
            'unexpected tables in native retrieval catalog: '
            + ', '.join(sorted(unexpected_tables))
        )

    for table, expected in _EXPECTED_COLUMNS.items():
        actual = tuple(
            row[1]
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            raise SchemaError(
                f'incompatible {table} columns: expected {expected}, got {actual}'
            )

    runtime_rows = [
        (row[0], row[1])
        for row in connection.execute(
            '''
            SELECT runtime_id, schema_version
            FROM retrieval_runtime
            '''
        )
    ]
    if runtime_rows != [(1, SCHEMA_VERSION)]:
        raise SchemaError(
            'retrieval_runtime must contain exactly the native singleton row'
        )


__all__ = [
    'MAX_SIGNED_INT64',
    'RETRIEVAL_TABLES',
    'SCHEMA_VERSION',
    'SchemaError',
    'checkpoint_isolated_catalog',
    'configure_catalog_storage',
    'install_schema',
    'require_main_file_only',
]
