from __future__ import annotations

import sqlite3

import pytest

from src.retrieval.schema import (
    RETRIEVAL_TABLES,
    SCHEMA_VERSION,
    SchemaError,
    configure_catalog_storage,
    install_schema,
    require_main_file_only,
)


PROFILE_HASH = '10' * 32
SOURCE_HASH_A = '20' * 32
SOURCE_HASH_B = '21' * 32
METADATA_HASH_A = '30' * 32
METADATA_HASH_B = '31' * 32
REPORT_UID_A = '40' * 32
REPORT_UID_B = '41' * 32
PARENT_UID_A = '50' * 32
PARENT_UID_B = '51' * 32
CHUNK_UID_A = '60' * 32
CHUNK_UID_B = '61' * 32
CHUNK_UID_C = '62' * 32
MANIFEST_HASH = '70' * 32
SNAPSHOT_HASH = '80' * 32


@pytest.fixture
def connection():
    database = sqlite3.connect(':memory:')
    install_schema(database)
    try:
        yield database
    finally:
        database.close()


def _insert_profile(connection: sqlite3.Connection) -> None:
    connection.execute(
        '''
        INSERT INTO embedding_profiles (
            profile_id,
            profile_hash,
            model,
            dimension,
            metric,
            normalization,
            prefix_template,
            extractor,
            parent_policy_json,
            child_policy_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            'profile-v2',
            PROFILE_HASH,
            'test/model',
            3,
            'l2',
            1,
            '[Company: {target_name}, Title: {title}]\n',
            'test-extractor',
            '{"overlap":0,"size":100}',
            '{"overlap":1,"size":6}',
        ),
    )


def _insert_report(
    connection: sqlite3.Connection,
    *,
    report_uid: str = REPORT_UID_A,
    source_sha256: str = SOURCE_HASH_A,
    metadata_sha256: str = METADATA_HASH_A,
    title: str = 'Original',
) -> int:
    cursor = connection.execute(
        '''
        INSERT INTO reports (
            report_uid,
            canonical_relative_path,
            source_sha256,
            retrieval_metadata_sha256,
            report_type,
            report_date,
            target_name,
            title,
            broker
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            report_uid,
            'reports/2026/company-a.pdf',
            source_sha256,
            metadata_sha256,
            'company',
            '2026-07-16',
            'Company A',
            title,
            'Test Broker',
        ),
    )
    return int(cursor.lastrowid)


def _insert_parent_and_chunk(
    connection: sqlite3.Connection,
    *,
    report_id: int,
    parent_uid: str,
    chunk_uid: str,
    parent_order: int = 0,
    content: str = 'abcdef',
    span_start: int = 0,
    span_end: int = 3,
) -> None:
    connection.execute(
        '''
        INSERT INTO retrieval_parents (
            parent_uid,
            report_id,
            profile_id,
            parent_order,
            content,
            content_sha256
        ) VALUES (?, ?, 'profile-v2', ?, ?, ?)
        ''',
        (parent_uid, report_id, parent_order, content, '90' * 32),
    )
    connection.execute(
        '''
        INSERT INTO retrieval_chunks (
            chunk_uid,
            parent_uid,
            profile_id,
            child_order,
            span_start,
            span_end,
            embedding_text_sha256
        ) VALUES (?, ?, 'profile-v2', 0, ?, ?, ?)
        ''',
        (chunk_uid, parent_uid, span_start, span_end, '91' * 32),
    )


def _insert_build(connection: sqlite3.Connection, build_id: str = 'build-v2') -> None:
    connection.execute(
        '''
        INSERT INTO retrieval_builds (
            build_id,
            profile_id,
            source_manifest_json,
            source_manifest_sha256,
            included_count,
            excluded_count,
            expected_count,
            exclusion_policy_version
        ) VALUES (?, 'profile-v2', ?, ?, 1, 1, 2, 'source-exclusions-v1')
        ''',
        (
            build_id,
            '{"counts":{"discovered":2,"excluded":1,"included":1}}',
            MANIFEST_HASH,
        ),
    )


def _advance_build(
    connection: sqlite3.Connection,
    build_id: str,
    states: tuple[str, ...],
) -> None:
    for state in states:
        connection.execute(
            'UPDATE retrieval_builds SET state = ? WHERE build_id = ?',
            (state, build_id),
        )


def _insert_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str = 'snapshot-v2',
    build_id: str = 'build-v2',
    ntotal: int = 1,
) -> None:
    connection.execute(
        '''
        INSERT INTO vector_snapshots (
            snapshot_id,
            build_id,
            relative_path,
            file_sha256,
            size_bytes,
            dimension,
            metric,
            ntotal
        ) VALUES (?, ?, ?, ?, 128, 3, 'l2', ?)
        ''',
        (
            snapshot_id,
            build_id,
            f'retrieval/v2/snapshots/{snapshot_id}.faiss',
            SNAPSHOT_HASH,
            ntotal,
        ),
    )


def test_schema_contains_exactly_the_nine_native_tables_and_active_view(connection):
    tables = {
        row[0]
        for row in connection.execute(
            '''
            SELECT name
            FROM sqlite_schema
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            '''
        )
    }
    views = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'view'"
        )
    }

    assert tables == RETRIEVAL_TABLES
    assert views == {'active_reports'}
    assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1


def test_native_schema_has_no_legacy_tables_or_columns(connection):
    table_columns = {
        table: {
            row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        for table in RETRIEVAL_TABLES
    }

    assert 'parent_chunks' not in table_columns
    assert 'report_revisions' not in table_columns
    assert 'is_embedded' not in table_columns['reports']
    assert 'file_name' not in table_columns['reports']
    assert 'content' not in table_columns['retrieval_chunks']
    assert not any(
        column.startswith('legacy_')
        for columns in table_columns.values()
        for column in columns
    )


def test_report_path_is_non_unique_but_report_uid_and_source_identity_are_unique(
    connection,
):
    original_id = _insert_report(connection)
    corrected_id = _insert_report(
        connection,
        report_uid=REPORT_UID_B,
        source_sha256=SOURCE_HASH_B,
        metadata_sha256=METADATA_HASH_B,
        title='Corrected',
    )

    assert original_id != corrected_id
    assert connection.execute(
        '''
        SELECT count(*)
        FROM reports
        WHERE canonical_relative_path = 'reports/2026/company-a.pdf'
        '''
    ).fetchone()[0] == 2

    indexes = {
        row[1]: row[2] for row in connection.execute('PRAGMA index_list(reports)')
    }
    assert indexes['idx_reports_canonical_relative_path'] == 0
    assert any(unique for unique in indexes.values())

    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        _insert_report(
            connection,
            report_uid=REPORT_UID_A,
            source_sha256='22' * 32,
            metadata_sha256='32' * 32,
        )
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        _insert_report(
            connection,
            report_uid='42' * 32,
            source_sha256=SOURCE_HASH_A,
            metadata_sha256=METADATA_HASH_A,
        )
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        connection.execute(
            "UPDATE reports SET title = 'Mutated' WHERE report_id = ?",
            (original_id,),
        )


@pytest.mark.parametrize(
    'path',
    [
        '/absolute/report.pdf',
        'C:/reports/report.pdf',
        '..\\report.pdf',
        '../report.pdf',
        'reports/../../report.pdf',
    ],
)
def test_reports_reject_absolute_or_traversing_paths(connection, path):
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        connection.execute(
            '''
            INSERT INTO reports (
                report_uid,
                canonical_relative_path,
                source_sha256,
                retrieval_metadata_sha256,
                report_type,
                report_date,
                title,
                broker
            ) VALUES (?, ?, ?, ?, 'company', '2026-07-16', 'Title', 'Broker')
            ''',
            (REPORT_UID_A, path, SOURCE_HASH_A, METADATA_HASH_A),
        )


def test_foreign_keys_chunk_spans_membership_and_state_constraints(connection):
    _insert_profile(connection)
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        connection.execute(
            '''
            INSERT INTO retrieval_parents (
                parent_uid,
                report_id,
                profile_id,
                parent_order,
                content,
                content_sha256
            ) VALUES (?, 999, 'profile-v2', 0, 'abcdef', ?)
            ''',
            (PARENT_UID_A, '90' * 32),
        )

    report_id = _insert_report(connection)
    connection.execute(
        '''
        INSERT INTO retrieval_parents (
            parent_uid,
            report_id,
            profile_id,
            parent_order,
            content,
            content_sha256
        ) VALUES (?, ?, 'profile-v2', 0, 'abcdef', ?)
        ''',
        (PARENT_UID_A, report_id, '90' * 32),
    )

    with pytest.raises(sqlite3.IntegrityError, match='canonical parent'):
        connection.execute(
            '''
            INSERT INTO retrieval_chunks (
                chunk_uid,
                parent_uid,
                profile_id,
                child_order,
                span_start,
                span_end,
                embedding_text_sha256
            ) VALUES (?, ?, 'profile-v2', 0, 0, 7, ?)
            ''',
            (CHUNK_UID_A, PARENT_UID_A, '91' * 32),
        )
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        connection.execute(
            '''
            INSERT INTO retrieval_chunks (
                chunk_uid,
                parent_uid,
                profile_id,
                child_order,
                span_start,
                span_end,
                embedding_text_sha256
            ) VALUES (?, ?, 'profile-v2', 0, 2, 2, ?)
            ''',
            (CHUNK_UID_A, PARENT_UID_A, '91' * 32),
        )

    for chunk_uid, child_order, start, end in (
        (CHUNK_UID_A, 0, 0, 2),
        (CHUNK_UID_B, 1, 2, 4),
        (CHUNK_UID_C, 2, 4, 6),
    ):
        connection.execute(
            '''
            INSERT INTO retrieval_chunks (
                chunk_uid,
                parent_uid,
                profile_id,
                child_order,
                span_start,
                span_end,
                embedding_text_sha256
            ) VALUES (?, ?, 'profile-v2', ?, ?, ?, ?)
            ''',
            (chunk_uid, PARENT_UID_A, child_order, start, end, '91' * 32),
        )

    _insert_build(connection)
    _advance_build(
        connection,
        'build-v2',
        ('cataloging', 'vector_building', 'validating'),
    )
    _insert_snapshot(connection, ntotal=2)
    connection.execute(
        '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
           VALUES ('snapshot-v2', ?, 1)''',
        (CHUNK_UID_A,),
    )

    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        connection.execute(
            '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
               VALUES ('snapshot-v2', ?, 2)''',
            (CHUNK_UID_A,),
        )
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        connection.execute(
            '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
               VALUES ('snapshot-v2', ?, 1)''',
            (CHUNK_UID_B,),
        )
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        connection.execute(
            '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
               VALUES ('snapshot-v2', ?, 0)''',
            (CHUNK_UID_B,),
        )
    with pytest.raises(sqlite3.IntegrityError, match='ID range'):
        connection.execute(
            '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
               VALUES ('snapshot-v2', ?, 3)''',
            (CHUNK_UID_B,),
        )

    connection.execute(
        '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
           VALUES ('snapshot-v2', ?, 2)''',
        (CHUNK_UID_B,),
    )
    connection.execute(
        "UPDATE vector_snapshots SET state = 'validating' WHERE snapshot_id = 'snapshot-v2'"
    )
    connection.execute(
        "UPDATE vector_snapshots SET state = 'ready' WHERE snapshot_id = 'snapshot-v2'"
    )

    with pytest.raises(sqlite3.IntegrityError, match='mutable snapshot'):
        connection.execute(
            '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
               VALUES ('snapshot-v2', ?, 2)''',
            (CHUNK_UID_C,),
        )
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        connection.execute(
            '''UPDATE snapshot_membership SET faiss_id = 2
               WHERE snapshot_id = 'snapshot-v2' AND chunk_uid = ?''',
            (CHUNK_UID_A,),
        )
    with pytest.raises(sqlite3.IntegrityError, match='illegal vector snapshot'):
        connection.execute(
            "UPDATE vector_snapshots SET state = 'validating' WHERE snapshot_id = 'snapshot-v2'"
        )

    _advance_build(connection, 'build-v2', ('ready',))
    with pytest.raises(sqlite3.IntegrityError, match='illegal retrieval build'):
        connection.execute(
            "UPDATE retrieval_builds SET state = 'cataloging' WHERE build_id = 'build-v2'"
        )
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        connection.execute(
            "UPDATE embedding_profiles SET model = 'other' WHERE profile_id = 'profile-v2'"
        )


def test_snapshot_cannot_be_ready_until_membership_equals_ntotal(connection):
    _insert_profile(connection)
    report_id = _insert_report(connection)
    _insert_parent_and_chunk(
        connection,
        report_id=report_id,
        parent_uid=PARENT_UID_A,
        chunk_uid=CHUNK_UID_A,
    )
    _insert_build(connection)
    _advance_build(
        connection,
        'build-v2',
        ('cataloging', 'vector_building', 'validating'),
    )
    _insert_snapshot(connection, ntotal=1)
    connection.execute(
        "UPDATE vector_snapshots SET state = 'validating' WHERE snapshot_id = 'snapshot-v2'"
    )

    with pytest.raises(sqlite3.IntegrityError, match='membership count'):
        connection.execute(
            "UPDATE vector_snapshots SET state = 'ready' WHERE snapshot_id = 'snapshot-v2'"
        )


def test_runtime_is_singleton_and_epoch_and_fallback_cannot_move_backward(connection):
    assert connection.execute(
        '''
        SELECT runtime_id, schema_version, publication_generation, write_epoch,
               v1_fallback_open, degraded, write_enabled
        FROM retrieval_runtime
        '''
    ).fetchall() == [(1, SCHEMA_VERSION, 0, 0, 1, 0, 0)]

    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        connection.execute(
            'INSERT INTO retrieval_runtime (runtime_id) VALUES (2)'
        )
    with pytest.raises(sqlite3.IntegrityError, match='cannot be deleted'):
        connection.execute('DELETE FROM retrieval_runtime WHERE runtime_id = 1')

    connection.execute(
        '''
        UPDATE retrieval_runtime
        SET publication_generation = 2,
            write_epoch = 1,
            v1_fallback_open = 0
        WHERE runtime_id = 1
        '''
    )
    with pytest.raises(sqlite3.IntegrityError, match='monotonic'):
        connection.execute(
            'UPDATE retrieval_runtime SET publication_generation = 1 WHERE runtime_id = 1'
        )
    with pytest.raises(sqlite3.IntegrityError, match='monotonic'):
        connection.execute(
            'UPDATE retrieval_runtime SET write_epoch = 0 WHERE runtime_id = 1'
        )
    with pytest.raises(sqlite3.IntegrityError, match='monotonic'):
        connection.execute(
            'UPDATE retrieval_runtime SET v1_fallback_open = 1 WHERE runtime_id = 1'
        )


def test_publication_phase_and_terminal_state_are_monotonic(connection):
    connection.execute(
        "INSERT INTO publication_runs (publication_id) VALUES ('publication-v2')"
    )
    connection.execute(
        '''
        UPDATE publication_runs
        SET phase = 'artifact_written'
        WHERE publication_id = 'publication-v2'
        '''
    )

    with pytest.raises(sqlite3.IntegrityError, match='cannot move backward'):
        connection.execute(
            '''
            UPDATE publication_runs
            SET phase = 'catalog_written'
            WHERE publication_id = 'publication-v2'
            '''
        )

    connection.execute(
        '''
        UPDATE publication_runs
        SET state = 'failed', error_code = 'artifact_validation_failed'
        WHERE publication_id = 'publication-v2'
        '''
    )
    with pytest.raises(sqlite3.IntegrityError, match='terminal'):
        connection.execute(
            '''
            UPDATE publication_runs
            SET state = 'running'
            WHERE publication_id = 'publication-v2'
            '''
        )


def test_active_reports_contains_only_objects_reachable_from_active_snapshot(
    connection,
):
    _insert_profile(connection)
    old_report_id = _insert_report(connection)
    corrected_report_id = _insert_report(
        connection,
        report_uid=REPORT_UID_B,
        source_sha256=SOURCE_HASH_B,
        metadata_sha256=METADATA_HASH_B,
        title='Corrected',
    )
    _insert_parent_and_chunk(
        connection,
        report_id=old_report_id,
        parent_uid=PARENT_UID_A,
        chunk_uid=CHUNK_UID_A,
    )
    _insert_parent_and_chunk(
        connection,
        report_id=corrected_report_id,
        parent_uid=PARENT_UID_B,
        chunk_uid=CHUNK_UID_B,
        parent_order=0,
    )
    _insert_build(connection)
    _advance_build(
        connection,
        'build-v2',
        ('cataloging', 'vector_building', 'validating'),
    )
    _insert_snapshot(connection)
    connection.execute(
        '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
           VALUES ('snapshot-v2', ?, 1)''',
        (CHUNK_UID_B,),
    )
    connection.execute(
        "UPDATE vector_snapshots SET state = 'validating' WHERE snapshot_id = 'snapshot-v2'"
    )
    connection.execute(
        "UPDATE vector_snapshots SET state = 'ready' WHERE snapshot_id = 'snapshot-v2'"
    )
    _advance_build(
        connection,
        'build-v2',
        ('ready', 'committed_pending_checkpoint', 'fully_complete'),
    )
    connection.execute(
        '''
        UPDATE retrieval_runtime
        SET active_snapshot_id = 'snapshot-v2',
            active_build_id = 'build-v2',
            publication_generation = 1
        WHERE runtime_id = 1
        '''
    )

    active = connection.execute(
        '''
        SELECT report_id, report_uid, canonical_relative_path, title
        FROM active_reports
        '''
    ).fetchall()
    assert active == [
        (
            corrected_report_id,
            REPORT_UID_B,
            'reports/2026/company-a.pdf',
            'Corrected',
        )
    ]
    assert old_report_id not in {row[0] for row in active}

    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        connection.execute(
            '''
            UPDATE retrieval_runtime
            SET predecessor_snapshot_id = active_snapshot_id
            WHERE runtime_id = 1
            '''
        )


def test_repeated_schema_install_is_a_schema_and_data_no_op(tmp_path):
    database_path = tmp_path / 'catalog.sqlite3'
    connection = sqlite3.connect(database_path)
    install_schema(connection)
    _insert_report(connection)
    connection.commit()

    schema_before = connection.execute(
        '''
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        ORDER BY type, name
        '''
    ).fetchall()
    data_before = tuple(connection.iterdump())
    changes_before = connection.total_changes

    install_schema(connection)

    assert connection.total_changes == changes_before
    assert connection.execute(
        '''
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        ORDER BY type, name
        '''
    ).fetchall() == schema_before
    assert tuple(connection.iterdump()) == data_before
    assert connection.execute(
        'SELECT count(*) FROM retrieval_runtime'
    ).fetchone()[0] == 1
    assert connection.execute('SELECT count(*) FROM reports').fetchone()[0] == 1
    connection.close()


def test_file_backed_native_catalog_uses_wal_with_full_durability(tmp_path):
    database_path = tmp_path / 'catalog.sqlite3'
    connection = sqlite3.connect(database_path)

    install_schema(connection)

    assert connection.execute('PRAGMA journal_mode').fetchone() == ('wal',)
    assert connection.execute('PRAGMA synchronous').fetchone() == (2,)
    connection.close()

    reopened = sqlite3.connect(database_path)
    assert reopened.execute('PRAGMA journal_mode').fetchone() == ('wal',)
    reopened.close()


def test_catalog_storage_assertion_does_not_transition_rollback_journal(tmp_path):
    database_path = tmp_path / 'legacy-storage.sqlite3'
    connection = sqlite3.connect(database_path)
    connection.execute('CREATE TABLE existing (value INTEGER)')
    connection.commit()
    connection.close()

    read_only = sqlite3.connect(
        f'file:{database_path.as_posix()}?mode=ro',
        uri=True,
    )
    with pytest.raises(SchemaError, match='must use WAL'):
        configure_catalog_storage(read_only)
    read_only.close()

    check = sqlite3.connect(database_path)
    assert check.execute('PRAGMA journal_mode').fetchone() == ('delete',)
    check.close()


def test_main_file_only_rejects_nonempty_sqlite_sidecar(tmp_path):
    catalog = tmp_path / 'catalog.sqlite3'
    catalog.write_bytes(b'catalog')
    wal = tmp_path / 'catalog.sqlite3-wal'
    wal.write_bytes(b'uncheckpointed')

    with pytest.raises(SchemaError, match='nonempty -wal sidecar'):
        require_main_file_only(catalog)

    assert wal.read_bytes() == b'uncheckpointed'


def test_main_file_only_removes_empty_sqlite_sidecars(tmp_path):
    catalog = tmp_path / 'catalog.sqlite3'
    catalog.write_bytes(b'catalog')
    sidecars = [
        tmp_path / f'catalog.sqlite3{suffix}'
        for suffix in ('-wal', '-shm', '-journal')
    ]
    for sidecar in sidecars:
        sidecar.touch()

    require_main_file_only(catalog)

    assert not any(sidecar.exists() for sidecar in sidecars)
