from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from src.retrieval.delta_schema import install_delta_schema
from src.retrieval.reader import NativeRetrievalReader
from src.retrieval.repository import CatalogRepository, compile_scope_filters
from src.retrieval.schema import configure_catalog_storage
from src.retrieval.vector_index import build_index
from tests.retrieval.test_retrieval_repository import _create_catalog, _digest


@dataclass(frozen=True)
class _DeltaChange:
    action: str
    canonical_path: str
    body: str | None = None
    vector: tuple[float, float] | None = None


def _publish_delta(
    catalog_path: Path,
    root: Path,
    *,
    sequence: int,
    changes: tuple[_DeltaChange, ...],
) -> dict[str, str]:
    fingerprint = '|'.join(
        f'{change.action}:{change.canonical_path}:{change.body}'
        for change in changes
    )
    segment_id = _digest(f'segment-{sequence}-{fingerprint}')
    upserts = tuple(change for change in changes if change.action == 'upsert')
    relative_path = f'deltas/{segment_id}.faiss' if upserts else None
    descriptor = (
        build_index(
            np.asarray([change.vector for change in upserts], dtype=np.float32),
            range(1, len(upserts) + 1),
            metric='l2',
        ).write(root / relative_path)
        if relative_path is not None
        else None
    )
    report_uids: dict[str, str] = {}

    connection = sqlite3.connect(catalog_path)
    configure_catalog_storage(connection, writable=True)
    connection.execute('PRAGMA foreign_keys = ON')
    try:
        install_delta_schema(connection)
        connection.execute('BEGIN IMMEDIATE')
        next_report_id = int(
            connection.execute('SELECT MAX(report_id) + 1 FROM reports').fetchone()[0]
        )
        for offset, change in enumerate(upserts):
            assert change.body is not None
            assert change.vector is not None
            report_uid = _digest(
                f'delta-report-{sequence}-{change.canonical_path}-{change.body}'
            )
            parent_uid = _digest(f'delta-parent-{report_uid}')
            chunk_uid = _digest(f'delta-chunk-{report_uid}')
            content = f'prefix::{change.body}::suffix'
            start = len('prefix::')
            connection.execute(
                '''
                INSERT INTO reports (
                    report_id, report_uid, canonical_relative_path, source_sha256,
                    retrieval_metadata_sha256, report_type, report_date,
                    target_name, title, broker
                ) VALUES (?, ?, ?, ?, ?, 'company', '2026-08-01',
                          'Alpha', 'Updated report', 'Broker A')
                ''',
                (
                    next_report_id + offset,
                    report_uid,
                    change.canonical_path,
                    _digest(f'source-{report_uid}'),
                    _digest(f'metadata-{report_uid}'),
                ),
            )
            connection.execute(
                '''
                INSERT INTO retrieval_parents (
                    parent_uid, report_id, profile_id, parent_order,
                    content, content_sha256
                ) VALUES (?, ?, 'profile-1', 0, ?, ?)
                ''',
                (parent_uid, next_report_id + offset, content, _digest(content)),
            )
            connection.execute(
                '''
                INSERT INTO retrieval_chunks (
                    chunk_uid, parent_uid, profile_id, child_order,
                    span_start, span_end, embedding_text_sha256
                ) VALUES (?, ?, 'profile-1', 0, ?, ?, ?)
                ''',
                (
                    chunk_uid,
                    parent_uid,
                    start,
                    start + len(change.body),
                    _digest(change.body),
                ),
            )
            report_uids[change.canonical_path] = report_uid
        connection.execute(
            '''
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal
            ) VALUES (?, 'snapshot-1', 7, ?, ?, ?, ?, 2, 'l2', ?)
            ''',
            (
                segment_id,
                sequence,
                relative_path,
                None if descriptor is None else descriptor.sha256,
                0 if descriptor is None else descriptor.size_bytes,
                len(upserts),
            ),
        )
        for change in changes:
            connection.execute(
                '''
                INSERT INTO retrieval_delta_reports (
                    segment_id, canonical_relative_path, action, report_uid
                ) VALUES (?, ?, ?, ?)
                ''',
                (
                    segment_id,
                    change.canonical_path,
                    change.action,
                    report_uids.get(change.canonical_path),
                ),
            )
        for faiss_id, change in enumerate(upserts, 1):
            report_uid = report_uids[change.canonical_path]
            connection.execute(
                '''
                INSERT INTO retrieval_delta_membership (segment_id, chunk_uid, faiss_id)
                VALUES (?, ?, ?)
                ''',
                (segment_id, _digest(f'delta-chunk-{report_uid}'), faiss_id),
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
    finally:
        connection.close()
    return report_uids


def _publish_upsert_delta(
    catalog_path: Path,
    root: Path,
    *,
    sequence: int,
    canonical_path: str,
    body: str,
    vector: tuple[float, float],
) -> str:
    return _publish_delta(
        catalog_path,
        root,
        sequence=sequence,
        changes=(_DeltaChange('upsert', canonical_path, body, vector),),
    )[canonical_path]


def test_upsert_delta_atomically_replaces_base_report_in_search(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    reader = NativeRetrievalReader(repository)

    before = reader.search(np.asarray([0.0, 5.0], dtype=np.float32), 1)
    assert before.results[0].parent_slice == rows[0]['body']

    new_uid = _publish_upsert_delta(
        catalog_path,
        tmp_path,
        sequence=1,
        canonical_path=str(rows[0]['path']),
        body='continuously visible replacement',
        vector=(0.0, 5.0),
    )
    after = reader.search(np.asarray([0.0, 5.0], dtype=np.float32), 5)

    assert after.snapshot_total == 5
    assert after.revision.delta_generation == 1
    assert after.revision.delta_segment_count == 1
    assert after.results[0].report_uid == new_uid
    assert after.results[0].parent_slice == 'continuously visible replacement'
    assert all(
        result.parent_slice != rows[0]['body']
        for result in after.results
    )


def test_open_request_keeps_its_composite_revision_when_delta_advances(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    query = np.asarray([0.0, 5.0], dtype=np.float32)

    with repository.request() as session:
        assert session.revision.delta_generation == 0
        new_uid = _publish_upsert_delta(
            catalog_path,
            tmp_path,
            sequence=1,
            canonical_path=str(rows[0]['path']),
            body='next request only',
            vector=(0.0, 5.0),
        )
        old_result = session.hydrate_search_batch(session.search_index(query, 1))
        assert old_result[0].report_uid != new_uid

    with repository.request() as session:
        assert session.revision.delta_generation == 1
        new_result = session.hydrate_search_batch(session.search_index(query, 1))
        assert new_result[0].report_uid == new_uid


def test_composite_scope_counts_only_the_visible_report_version(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    _publish_upsert_delta(
        catalog_path,
        tmp_path,
        sequence=1,
        canonical_path=str(rows[0]['path']),
        body='new dated version',
        vector=(0.0, 5.0),
    )
    repository = CatalogRepository(catalog_path, data_root=tmp_path)

    with repository.request() as session:
        old_date = compile_scope_filters({'report_date': '2026-07-01'})
        new_date = compile_scope_filters({'report_date': '2026-08-01'})
        assert session.eligible_count(old_date) == 0
        assert session.eligible_count(new_date) == 1


def test_multi_segment_heads_preserve_untouched_paths_and_hide_predecessors(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    path_a = str(rows[0]['path'])
    path_b = str(rows[1]['path'])

    _publish_delta(
        catalog_path,
        tmp_path,
        sequence=1,
        changes=(
            _DeltaChange('upsert', path_a, 'A from segment one', (0.0, 5.0)),
            _DeltaChange('upsert', path_b, 'B from segment one', (0.1, 4.9)),
        ),
    )
    _publish_delta(
        catalog_path,
        tmp_path,
        sequence=2,
        changes=(_DeltaChange('delete', path_a),),
    )

    reader = NativeRetrievalReader(CatalogRepository(catalog_path, data_root=tmp_path))
    after_delete = reader.search(np.asarray([0.0, 5.0], dtype=np.float32), 10)
    delete_bodies = {result.parent_slice for result in after_delete.results}
    assert after_delete.snapshot_total == 4
    assert after_delete.revision.delta_generation == 2
    assert 'B from segment one' in delete_bodies
    assert 'A from segment one' not in delete_bodies
    assert rows[0]['body'] not in delete_bodies
    assert rows[1]['body'] not in delete_bodies

    _publish_delta(
        catalog_path,
        tmp_path,
        sequence=3,
        changes=(
            _DeltaChange('upsert', path_a, 'A from segment three', (0.0, 5.0)),
        ),
    )
    after_reupsert = reader.search(np.asarray([0.0, 5.0], dtype=np.float32), 10)
    final_bodies = {result.parent_slice for result in after_reupsert.results}
    assert after_reupsert.snapshot_total == 5
    assert after_reupsert.revision.delta_generation == 3
    assert {'A from segment three', 'B from segment one'} <= final_bodies
    assert 'A from segment one' not in final_bodies
    assert rows[0]['body'] not in final_bodies
    assert rows[1]['body'] not in final_bodies


def test_delete_only_delta_is_request_pinned_and_hides_the_base_report(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    query = np.asarray([0.0, 5.0], dtype=np.float32)

    with repository.request() as pinned:
        before = pinned.hydrate_search_batch(pinned.search_index(query, 10))
        _publish_delta(
            catalog_path,
            tmp_path,
            sequence=1,
            changes=(_DeltaChange('delete', str(rows[0]['path'])),),
        )
        still_pinned = pinned.hydrate_search_batch(pinned.search_index(query, 10))
        assert pinned.revision.delta_generation == 0
        assert rows[0]['body'] in {result.parent_slice for result in before}
        assert rows[0]['body'] in {result.parent_slice for result in still_pinned}

    with repository.request() as current:
        after = current.hydrate_search_batch(current.search_index(query, 10))
        assert current.revision.delta_generation == 1
        assert current.total_count == 4
        assert rows[0]['body'] not in {result.parent_slice for result in after}
