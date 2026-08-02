from __future__ import annotations

import sqlite3

import pytest

from src.retrieval.delta_schema import (
    DELTA_GC_TABLES,
    DELTA_GC_TRIGGERS,
    DELTA_INDEXES,
    DELTA_TABLES,
    DELTA_TRIGGERS,
    delta_schema_installed,
    install_delta_schema,
)
from src.retrieval.schema import SchemaError, configure_catalog_storage
from tests.retrieval.test_retrieval_repository import _create_catalog, _digest


def _open_writable(path):
    connection = sqlite3.connect(path)
    configure_catalog_storage(connection, writable=True)
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def _insert_staged_segment(
    connection,
    *,
    segment_id,
    sequence,
    base_snapshot_id='snapshot-1',
    relative_path=None,
    file_sha256=None,
    size_bytes=0,
    ntotal=0,
):
    connection.execute(
        '''
        INSERT INTO retrieval_delta_segments (
            segment_id, base_snapshot_id, base_publication_generation,
            sequence, relative_path, file_sha256, size_bytes,
            dimension, metric, ntotal
        ) VALUES (?, ?, 7, ?, ?, ?, ?, 2, 'l2', ?)
        ''',
        (
            segment_id,
            base_snapshot_id,
            sequence,
            relative_path,
            file_sha256,
            size_bytes,
            ntotal,
        ),
    )


def test_delta_extension_is_optional_and_installs_idempotently(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        assert not delta_schema_installed(connection)

        install_delta_schema(connection)
        assert delta_schema_installed(connection)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert DELTA_TABLES.issubset(tables)

        install_delta_schema(connection)
        assert delta_schema_installed(connection)
    finally:
        connection.close()


def test_partial_delta_extension_fails_closed(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        connection.execute('CREATE TABLE retrieval_delta_segments (id TEXT)')
        with pytest.raises(SchemaError, match='partially installed'):
            delta_schema_installed(connection)
    finally:
        connection.close()


def test_fully_present_but_malformed_delta_extension_fails_closed(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        connection.execute('CREATE TABLE retrieval_delta_segments (id TEXT)')
        connection.execute('CREATE TABLE retrieval_delta_reports (id TEXT)')
        connection.execute('CREATE TABLE retrieval_delta_membership (id TEXT)')

        with pytest.raises(SchemaError, match='incompatible retrieval_delta_segments'):
            delta_schema_installed(connection)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("object_type", "name", "message"),
    (
        (
            "TRIGGER",
            next(iter(sorted(DELTA_TRIGGERS))),
            "missing required triggers",
        ),
        (
            "INDEX",
            next(iter(sorted(DELTA_INDEXES))),
            "missing required indexes",
        ),
    ),
)
def test_delta_extension_rejects_missing_required_schema_objects(
    tmp_path,
    object_type,
    name,
    message,
):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        connection.execute(f'DROP {object_type} "{name}"')

        with pytest.raises(SchemaError, match=message):
            delta_schema_installed(connection)
    finally:
        connection.close()


def test_delta_schema_installer_adds_the_gc_ledger_to_an_existing_extension(
    tmp_path,
):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        for trigger in DELTA_GC_TRIGGERS:
            connection.execute(f'DROP TRIGGER "{trigger}"')
        for table in DELTA_GC_TABLES:
            connection.execute(f'DROP TABLE "{table}"')

        assert delta_schema_installed(connection)
        install_delta_schema(connection)

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
            )
        }
        assert DELTA_GC_TABLES <= tables
        assert DELTA_GC_TRIGGERS <= triggers
    finally:
        connection.close()


def test_delta_extension_rejects_a_missing_gc_ledger_trigger(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        trigger = next(iter(sorted(DELTA_GC_TRIGGERS)))
        connection.execute(f'DROP TRIGGER "{trigger}"')

        with pytest.raises(SchemaError, match='GC schema is missing required triggers'):
            delta_schema_installed(connection)
    finally:
        connection.close()


def test_delete_only_delta_activates_atomically_and_is_immutable(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        segment_id = 'a' * 64
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            '''
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal
            ) VALUES (?, 'snapshot-1', 7, 1, NULL, NULL, 0, 2, 'l2', 0)
            ''',
            (segment_id,),
        )
        connection.execute(
            '''
            INSERT INTO retrieval_delta_reports (
                segment_id, canonical_relative_path, action, report_uid
            ) VALUES (?, ?, 'delete', NULL)
            ''',
            (segment_id, rows[0]['path']),
        )
        connection.execute(
            '''
            UPDATE retrieval_delta_segments
            SET state = 'ready',
                state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE segment_id = ?
            ''',
            (segment_id,),
        )
        connection.commit()

        assert connection.execute(
            'SELECT state FROM retrieval_delta_segments WHERE segment_id = ?',
            (segment_id,),
        ).fetchone()[0] == 'ready'
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            connection.execute(
                'UPDATE retrieval_delta_segments SET sequence = 2 WHERE segment_id = ?',
                (segment_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match='durable'):
            connection.execute(
                'DELETE FROM retrieval_delta_reports WHERE segment_id = ?',
                (segment_id,),
            )
    finally:
        connection.close()


def test_delta_membership_uses_declared_base_snapshot_profile(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        connection.execute(
            '''
            INSERT INTO embedding_profiles (
                profile_id, profile_hash, model, dimension, metric, normalization,
                prefix_template, extractor, parent_policy_json, child_policy_json
            ) VALUES ('profile-2', ?, 'test-model-2', 2, 'l2', 0,
                      '', 'test-extractor', '{}', '{}')
            ''',
            (_digest('profile-2'),),
        )
        connection.execute(
            '''
            INSERT INTO retrieval_builds (
                build_id, profile_id, source_manifest_json,
                source_manifest_sha256, included_count, excluded_count,
                expected_count, exclusion_policy_version
            ) VALUES ('build-2', 'profile-2', '{}', ?, 1, 0, 1, 'test-v1')
            ''',
            (_digest('manifest-2'),),
        )
        connection.execute(
            '''
            INSERT INTO vector_snapshots (
                snapshot_id, build_id, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal
            ) VALUES ('snapshot-2', 'build-2', 'snapshots/snapshot-2.faiss',
                      ?, 10, 2, 'l2', 1)
            ''',
            (_digest('snapshot-2-artifact'),),
        )

        report_uid, report_id = connection.execute(
            '''SELECT report_uid, report_id FROM reports
               WHERE canonical_relative_path = ?''',
            (rows[0]['path'],),
        ).fetchone()
        content = 'prefix::profile two child::suffix'
        parent_uid = _digest('profile-2-parent')
        chunk_uid = _digest('profile-2-chunk')
        connection.execute(
            '''
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order,
                content, content_sha256
            ) VALUES (?, ?, 'profile-2', 0, ?, ?)
            ''',
            (parent_uid, report_id, content, _digest(content)),
        )
        connection.execute(
            '''
            INSERT INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order,
                span_start, span_end, embedding_text_sha256
            ) VALUES (?, ?, 'profile-2', 0, 8, 25, ?)
            ''',
            (chunk_uid, parent_uid, _digest('profile two child')),
        )

        segment_id = _digest('profile-2-segment')
        _insert_staged_segment(
            connection,
            segment_id=segment_id,
            sequence=1,
            base_snapshot_id='snapshot-2',
            relative_path='deltas/profile-2.faiss',
            file_sha256=_digest('profile-2-delta-artifact'),
            size_bytes=10,
            ntotal=1,
        )
        connection.execute(
            '''
            INSERT INTO retrieval_delta_reports (
                segment_id, canonical_relative_path, action, report_uid
            ) VALUES (?, ?, 'upsert', ?)
            ''',
            (segment_id, rows[0]['path'], report_uid),
        )
        active_profile_chunk_uid = _digest('chunk-1')
        with pytest.raises(sqlite3.IntegrityError, match='profile'):
            connection.execute(
                '''INSERT INTO retrieval_delta_membership
                   (segment_id, chunk_uid, faiss_id) VALUES (?, ?, 1)''',
                (segment_id, active_profile_chunk_uid),
            )

        connection.execute(
            '''INSERT INTO retrieval_delta_membership
               (segment_id, chunk_uid, faiss_id) VALUES (?, ?, 1)''',
            (segment_id, chunk_uid),
        )
    finally:
        connection.close()


def test_ready_segment_descriptor_must_match_declared_base_snapshot(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        segment_id = _digest('wrong-dimension-segment')
        connection.execute(
            '''
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal
            ) VALUES (?, 'snapshot-1', 7, 1, NULL, NULL, 0, 3, 'l2', 0)
            ''',
            (segment_id,),
        )
        connection.execute(
            '''
            INSERT INTO retrieval_delta_reports (
                segment_id, canonical_relative_path, action, report_uid
            ) VALUES (?, ?, 'delete', NULL)
            ''',
            (segment_id, rows[0]['path']),
        )

        with pytest.raises(sqlite3.IntegrityError, match='descriptor'):
            connection.execute(
                "UPDATE retrieval_delta_segments SET state = 'ready' "
                'WHERE segment_id = ?',
                (segment_id,),
            )
    finally:
        connection.close()


@pytest.mark.parametrize('canonical_path', ('../x.pdf', 'a//b.pdf'))
def test_delete_action_rejects_noncanonical_path(tmp_path, canonical_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        segment_id = _digest(f'invalid-delete-{canonical_path}')
        _insert_staged_segment(connection, segment_id=segment_id, sequence=1)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                '''
                INSERT INTO retrieval_delta_reports (
                    segment_id, canonical_relative_path, action, report_uid
                ) VALUES (?, ?, 'delete', NULL)
                ''',
                (segment_id, canonical_path),
            )
    finally:
        connection.close()


@pytest.mark.parametrize(
    'relative_path',
    ('..', './delta.faiss', 'deltas//delta.faiss', 'deltas/./delta.faiss', 'deltas/'),
)
def test_segment_rejects_noncanonical_artifact_path(tmp_path, relative_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_staged_segment(
                connection,
                segment_id=_digest(f'invalid-artifact-{relative_path}'),
                sequence=1,
                relative_path=relative_path,
                file_sha256=_digest('invalid-artifact-bytes'),
                size_bytes=10,
                ntotal=1,
            )
    finally:
        connection.close()


def test_distinct_segments_may_share_identical_artifact_hashes(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = _open_writable(catalog_path)
    try:
        install_delta_schema(connection)
        shared_hash = _digest('identical-delta-bytes')
        _insert_staged_segment(
            connection,
            segment_id=_digest('same-bytes-segment-1'),
            sequence=1,
            relative_path='deltas/same-bytes-1.faiss',
            file_sha256=shared_hash,
            size_bytes=10,
            ntotal=1,
        )
        _insert_staged_segment(
            connection,
            segment_id=_digest('same-bytes-segment-2'),
            sequence=2,
            relative_path='deltas/same-bytes-2.faiss',
            file_sha256=shared_hash,
            size_bytes=10,
            ntotal=1,
        )

        assert connection.execute(
            '''SELECT count(*) FROM retrieval_delta_segments
               WHERE file_sha256 = ?''',
            (shared_hash,),
        ).fetchone()[0] == 2
    finally:
        connection.close()
