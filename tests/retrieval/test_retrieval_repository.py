from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from src.retrieval.repository import (
    CatalogRepository,
    CrossSnapshotMembershipError,
    LeaseReleasedError,
    RankedCandidate,
    RepositoryError,
    ScopeValidationError,
    SnapshotCache,
    SnapshotInUseError,
    SnapshotRevision,
    SnapshotValidationError,
    compile_scope_filters,
)
from src.retrieval.schema import install_schema
from src.retrieval.vector_index import SnapshotDescriptor, build_index


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _create_catalog(
    root: Path,
    *,
    count: int = 5,
    generation: int = 7,
) -> tuple[Path, list[dict[str, object]]]:
    rows = []
    vectors = []
    for offset in range(count):
        report_type = 'company' if offset < 3 else 'industry'
        target_name = 'Alpha' if offset in (0, 2, 3) else 'Beta'
        rows.append(
            {
                'report_type': report_type,
                'report_date': f'2026-07-{offset + 1:02d}',
                'target_name': target_name,
                'title': f'Report {offset + 1}',
                'broker': 'Broker A' if offset % 2 == 0 else 'Broker B',
                'path': f'reports/{report_type}-{offset + 1}.pdf',
                'body': f'native child body {offset + 1}',
            }
        )
        vectors.append([float(offset), float(count - offset)])

    snapshot_path = root / 'snapshots' / 'snapshot-1.faiss'
    descriptor = build_index(
        np.asarray(vectors, dtype=np.float32),
        range(1, count + 1),
        metric='l2',
    ).write(snapshot_path)
    catalog_path = root / 'catalog.sqlite3'
    connection = sqlite3.connect(catalog_path)
    install_schema(connection)
    connection.execute(
        '''
        INSERT INTO embedding_profiles (
            profile_id, profile_hash, model, dimension, metric, normalization,
            prefix_template, extractor, parent_policy_json, child_policy_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            'profile-1',
            _digest('profile-1'),
            'test-model',
            2,
            'l2',
            0,
            '',
            'test-extractor',
            '{}',
            '{}',
        ),
    )
    for index, row in enumerate(rows, 1):
        report_uid = _digest(f'report-{index}')
        parent_uid = _digest(f'parent-{index}')
        chunk_uid = _digest(f'chunk-{index}')
        body = str(row['body'])
        content = f'prefix::{body}::suffix'
        start = len('prefix::')
        end = start + len(body)
        connection.execute(
            '''
            INSERT INTO reports (
                report_id, report_uid, canonical_relative_path, source_sha256,
                retrieval_metadata_sha256, report_type, report_date,
                target_name, title, broker
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                index,
                report_uid,
                row['path'],
                _digest(f'source-{index}'),
                _digest(f'metadata-{index}'),
                row['report_type'],
                row['report_date'],
                row['target_name'],
                row['title'],
                row['broker'],
            ),
        )
        connection.execute(
            '''
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order, content,
                content_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (
                parent_uid,
                index,
                'profile-1',
                0,
                content,
                _digest(content),
            ),
        )
        connection.execute(
            '''
            INSERT INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order, span_start,
                span_end, embedding_text_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                chunk_uid,
                parent_uid,
                'profile-1',
                0,
                start,
                end,
                _digest(body),
            ),
        )

    connection.execute(
        '''
        INSERT INTO retrieval_builds (
            build_id, profile_id, source_manifest_json,
            source_manifest_sha256, included_count, excluded_count,
            expected_count, exclusion_policy_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            'build-1',
            'profile-1',
            '{}',
            _digest('manifest-1'),
            count,
            0,
            count,
            'test-v1',
        ),
    )
    connection.execute(
        '''
        INSERT INTO vector_snapshots (
            snapshot_id, build_id, relative_path, file_sha256, size_bytes,
            dimension, metric, ntotal
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            'snapshot-1',
            'build-1',
            'snapshots/snapshot-1.faiss',
            descriptor.sha256,
            descriptor.size_bytes,
            descriptor.dimension,
            descriptor.metric,
            descriptor.ntotal,
        ),
    )
    for physical_id in range(1, count + 1):
        connection.execute(
            '''INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
               VALUES (?, ?, ?)''',
            ('snapshot-1', _digest(f'chunk-{physical_id}'), physical_id),
        )

    for state in ('cataloging', 'vector_building', 'validating'):
        connection.execute(
            'UPDATE retrieval_builds SET state = ? WHERE build_id = ?',
            (state, 'build-1'),
        )
    for state in ('validating', 'ready'):
        connection.execute(
            'UPDATE vector_snapshots SET state = ? WHERE snapshot_id = ?',
            (state, 'snapshot-1'),
        )
    for state in ('ready', 'committed_pending_checkpoint', 'fully_complete'):
        connection.execute(
            'UPDATE retrieval_builds SET state = ? WHERE build_id = ?',
            (state, 'build-1'),
        )
    connection.execute(
        '''
        UPDATE retrieval_runtime
        SET active_snapshot_id = ?, active_build_id = ?,
            publication_generation = ?
        WHERE runtime_id = 1
        ''',
        ('snapshot-1', 'build-1', generation),
    )
    connection.commit()
    connection.close()
    return catalog_path, rows


def test_scope_compiler_binds_report_fields_and_prior_file_scope():
    hostile_target = "Alpha' OR 1=1 --"
    compiled = compile_scope_filters(
        {
            'company': hostile_target,
            'report_type': 'company',
            'report_date_start': '2026-07-01',
            'report_date_end': '2026-07-31',
            'broker': 'Broker A',
            'canonical_relative_paths': ['reports/company-1.pdf'],
            'prior_scope': {'file_names': ['company-1.pdf']},
        }
    )

    assert not compiled.is_empty
    assert not compiled.is_unfiltered
    assert hostile_target not in compiled.predicate_sql
    assert 'company-1.pdf' not in compiled.predicate_sql
    assert hostile_target in compiled.parameters
    assert 'report.target_name = ?' in compiled.predicate_sql
    assert 'report.report_date >= ?' in compiled.predicate_sql
    assert 'report.broker = ?' in compiled.predicate_sql
    assert 'report.canonical_relative_path' in compiled.predicate_sql


@pytest.mark.parametrize(
    'scope',
    [
        {'target_names': []},
        {'file_names': []},
        {'report_date_start': '2026-08-01', 'report_date_end': '2026-07-01'},
        {'empty': True},
    ],
)
def test_scope_compiler_proves_empty_scope_without_sql_values(scope):
    compiled = compile_scope_filters(scope)

    assert compiled.is_empty
    assert compiled.parameters == ()
    assert compiled.predicate_sql == '0 = 1'


def test_scope_compiler_fails_closed_on_unknown_field():
    with pytest.raises(ScopeValidationError, match='unsupported'):
        compile_scope_filters({'raw_sql': '1 = 1'})
    with pytest.raises(ScopeValidationError, match='prior_scope'):
        compile_scope_filters({'prior_scope': {'raw_sql': '1 = 1'}})


def test_request_releases_cache_lease_on_success_and_exception(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    cache = SnapshotCache()
    repository = CatalogRepository(catalog_path, data_root=tmp_path, cache=cache)

    with repository.request() as session:
        revision = session.revision
        assert revision.key == (7, 'snapshot-1')
        assert cache.lease_count(revision) == 1
    assert cache.lease_count(revision) == 0

    with pytest.raises(RuntimeError, match='request failure'):
        with repository.request() as session:
            assert cache.lease_count(session.revision) == 1
            raise RuntimeError('request failure')
    assert cache.lease_count(revision) == 0


def test_request_reads_active_revision_exactly_once(tmp_path, monkeypatch):
    catalog_path, _ = _create_catalog(tmp_path, generation=19)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    original = repository._read_active_revision
    calls = []

    def counted(connection):
        calls.append(True)
        return original(connection)

    monkeypatch.setattr(repository, '_read_active_revision', counted)
    with repository.request() as session:
        compiled = compile_scope_filters({'report_type': 'company'})
        assert session.eligible_count(compiled) == 3
        candidates = session.tag_results(
            session.index.search(np.asarray([0.0, 5.0], dtype=np.float32), 2)
        )
        session.hydrate(candidates)
        assert session.revision.publication_generation == 19

    assert len(calls) == 1


def test_repository_reuses_thread_connection_and_observes_new_generation(
    tmp_path,
    monkeypatch,
):
    catalog_path, _ = _create_catalog(tmp_path, generation=7)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    original = repository._open_read_only_connection
    opened = []

    def counted():
        opened.append(True)
        return original()

    monkeypatch.setattr(repository, '_open_read_only_connection', counted)
    with repository.request() as session:
        assert session.revision.publication_generation == 7

    connection = sqlite3.connect(catalog_path)
    connection.execute(
        '''UPDATE retrieval_runtime
           SET publication_generation = 8
           WHERE runtime_id = 1'''
    )
    connection.commit()
    connection.close()

    with repository.request() as session:
        assert session.revision.publication_generation == 8

    assert len(opened) == 1
    repository.close()


def test_closed_repository_rejects_new_requests(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    with repository.request():
        pass

    repository.close()

    with pytest.raises(RepositoryError, match='closed'):
        with repository.request():
            pass


def test_cache_keys_same_snapshot_by_publication_generation(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path, generation=7)
    cache = SnapshotCache()
    repository = CatalogRepository(catalog_path, data_root=tmp_path, cache=cache)
    with repository.request() as session:
        assert session.index.ntotal == 5
        first_revision = session.revision

    connection = sqlite3.connect(catalog_path)
    connection.execute(
        '''UPDATE retrieval_runtime
           SET publication_generation = 8
           WHERE runtime_id = 1'''
    )
    connection.commit()
    connection.close()

    with repository.request() as session:
        assert session.index.ntotal == 5
        second_revision = session.revision

    assert first_revision.key == (7, 'snapshot-1')
    assert second_revision.key == (8, 'snapshot-1')
    assert set(cache.cached_revisions()) == {first_revision, second_revision}


def test_hydration_cache_does_not_cross_publication_generation(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path, generation=7)
    cache = SnapshotCache()
    repository = CatalogRepository(catalog_path, data_root=tmp_path, cache=cache)
    with repository.request() as session:
        session.hydrate(
            (
                RankedCandidate(
                    snapshot_id=session.revision.snapshot_id,
                    publication_generation=session.revision.publication_generation,
                    physical_id=1,
                    score=0.1,
                ),
            )
        )

    connection = sqlite3.connect(catalog_path)
    connection.execute(
        '''UPDATE retrieval_runtime
           SET publication_generation = 8
           WHERE runtime_id = 1'''
    )
    connection.commit()
    connection.close()

    with repository.request() as session:
        session.hydrate(
            (
                RankedCandidate(
                    snapshot_id=session.revision.snapshot_id,
                    publication_generation=session.revision.publication_generation,
                    physical_id=1,
                    score=0.2,
                ),
            )
        )
        assert session.hydration_cache_hits == 0
        assert session.hydration_cache_misses == 1
        assert session.hydration_sql_rows == 1

    repository.close()
    cache.close()


def test_leased_search_batch_preserves_revision_and_cannot_cross_generation(
    tmp_path,
):
    catalog_path, _ = _create_catalog(tmp_path, generation=7)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    with repository.request() as session:
        batch = session.search_index(
            np.asarray([0.0, 5.0], dtype=np.float32),
            2,
        )
        hydrated = session.hydrate_search_batch(batch)

    assert [result.physical_id for result in hydrated] == [1, 2]
    connection = sqlite3.connect(catalog_path)
    connection.execute(
        '''UPDATE retrieval_runtime
           SET publication_generation = 8
           WHERE runtime_id = 1'''
    )
    connection.commit()
    connection.close()

    with repository.request() as session:
        with pytest.raises(CrossSnapshotMembershipError, match='different'):
            session.hydrate_search_batch(batch)

    repository.close()


def test_session_cannot_be_used_after_context_release(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    with repository.request() as session:
        compiled = compile_scope_filters(None)

    with pytest.raises(LeaseReleasedError):
        session.eligible_count(compiled)


def test_hydration_preserves_rank_and_returns_parent_slice_and_logical_ids(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path)
    repository = CatalogRepository(
        catalog_path,
        data_root=tmp_path,
        query_batch_size=2,
    )
    with repository.request() as session:
        candidates = tuple(
            RankedCandidate(
                snapshot_id=session.revision.snapshot_id,
                publication_generation=session.revision.publication_generation,
                physical_id=physical_id,
                score=score,
            )
            for physical_id, score in ((4, 0.1), (1, 0.2), (3, 0.3))
        )
        hydrated = session.hydrate(candidates)

    assert [result.physical_id for result in hydrated] == [4, 1, 3]
    assert [result.rank for result in hydrated] == [1, 2, 3]
    assert hydrated[0].parent_slice == rows[3]['body']
    assert hydrated[0].text == rows[3]['body']
    assert hydrated[0].chunk_uid == _digest('chunk-4')
    assert hydrated[0].parent_uid == _digest('parent-4')
    assert hydrated[0].report_uid == _digest('report-4')
    assert hydrated[0].metadata['canonical_relative_path'] == rows[3]['path']
    with pytest.raises(AttributeError):
        hydrated[0].rank = 99


def test_hydration_reuses_revision_cache_without_reusing_rank_or_score(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    with repository.request() as session:
        first = session.hydrate(
            tuple(
                RankedCandidate(
                    snapshot_id=session.revision.snapshot_id,
                    publication_generation=session.revision.publication_generation,
                    physical_id=physical_id,
                    score=score,
                )
                for physical_id, score in ((1, 0.1), (3, 0.3))
            )
        )
    assert [item.physical_id for item in first] == [1, 3]

    statements = []
    connection = next(iter(repository._connections.values()))
    connection.set_trace_callback(statements.append)
    with repository.request() as session:
        second = session.hydrate(
            tuple(
                RankedCandidate(
                    snapshot_id=session.revision.snapshot_id,
                    publication_generation=session.revision.publication_generation,
                    physical_id=physical_id,
                    score=score,
                )
                for physical_id, score in ((3, 0.9), (1, 0.8))
            )
        )
        assert session.hydration_cache_hits == 2
        assert session.hydration_cache_misses == 0
        assert session.hydration_sql_batches == 0
        assert session.hydration_sql_rows == 0

    assert [item.physical_id for item in second] == [3, 1]
    assert [item.rank for item in second] == [1, 2]
    assert [item.score for item in second] == [0.9, 0.8]
    assert any('from retrieval_runtime' in statement.lower() for statement in statements)
    assert not any('substr(' in statement.lower() for statement in statements)
    repository.close()


def test_hydration_cache_is_bounded_and_evicts_least_recently_used(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    cache = SnapshotCache(hydration_cache_size=2)
    repository = CatalogRepository(
        catalog_path,
        data_root=tmp_path,
        cache=cache,
    )

    def candidates(session, *physical_ids):
        return tuple(
            RankedCandidate(
                snapshot_id=session.revision.snapshot_id,
                publication_generation=session.revision.publication_generation,
                physical_id=physical_id,
                score=float(physical_id),
            )
            for physical_id in physical_ids
        )

    with repository.request() as session:
        session.hydrate(candidates(session, 1, 2))
    with repository.request() as session:
        session.hydrate(candidates(session, 1))
        session.hydrate(candidates(session, 3))
    with repository.request() as session:
        session.hydrate(candidates(session, 1, 2))
        assert session.hydration_cache_hits == 1
        assert session.hydration_cache_misses == 1
        assert session.hydration_sql_rows == 1

    repository.close()
    cache.close()


def test_hydration_rejects_candidate_tagged_with_another_snapshot(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    with repository.request() as session:
        foreign = RankedCandidate(
            snapshot_id='snapshot-2',
            publication_generation=session.revision.publication_generation,
            physical_id=1,
            score=0.0,
        )

        with pytest.raises(CrossSnapshotMembershipError, match='different'):
            session.hydrate((foreign,))


def test_repository_rejects_catalog_membership_descriptor_mismatch(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    connection = sqlite3.connect(catalog_path)
    connection.execute('DROP TRIGGER snapshot_membership_no_delete')
    connection.execute(
        '''DELETE FROM snapshot_membership
           WHERE snapshot_id = 'snapshot-1' AND faiss_id = 5'''
    )
    connection.commit()
    connection.close()
    repository = CatalogRepository(catalog_path, data_root=tmp_path)

    with pytest.raises(SnapshotValidationError, match='membership'):
        with repository.request():
            pass


def test_repository_rejects_snapshot_bytes_that_do_not_match_descriptor(tmp_path):
    catalog_path, _ = _create_catalog(tmp_path)
    snapshot_path = tmp_path / 'snapshots' / 'snapshot-1.faiss'
    snapshot_path.write_bytes(snapshot_path.read_bytes() + b'tampered')
    repository = CatalogRepository(catalog_path, data_root=tmp_path)

    with pytest.raises(SnapshotValidationError, match='size|hash'):
        with repository.request():
            pass


def test_cache_blocks_evict_and_close_until_lease_releases(tmp_path):
    descriptor = SnapshotDescriptor(
        sha256='a' * 64,
        size_bytes=1,
        dimension=2,
        metric='l2',
        ntotal=3,
    )
    revision = SnapshotRevision(
        catalog_path=tmp_path / 'catalog.sqlite3',
        publication_generation=1,
        snapshot_id='snapshot-1',
        build_id='build-1',
        profile_id='profile-1',
        snapshot_path=tmp_path / 'snapshot.faiss',
        descriptor=descriptor,
    )

    class FakeIndex:
        dimension = 2
        metric = 'l2'
        ntotal = 3

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    fake = FakeIndex()
    cache = SnapshotCache(loader=lambda _path, _descriptor: fake)
    lease = cache.acquire(revision)
    assert lease.__enter__() is fake

    with pytest.raises(SnapshotInUseError):
        cache.evict(revision)
    with pytest.raises(SnapshotInUseError):
        cache.close()
    assert not fake.closed

    lease.__exit__(None, None, None)
    assert cache.evict(revision)
    assert fake.closed


def test_cache_loads_one_index_for_concurrent_revision_leases(tmp_path):
    descriptor = SnapshotDescriptor(
        sha256='b' * 64,
        size_bytes=1,
        dimension=2,
        metric='l2',
        ntotal=3,
    )
    revision = SnapshotRevision(
        catalog_path=tmp_path / 'catalog.sqlite3',
        publication_generation=3,
        snapshot_id='snapshot-3',
        build_id='build-3',
        profile_id='profile-1',
        snapshot_path=tmp_path / 'snapshot-3.faiss',
        descriptor=descriptor,
    )

    class FakeIndex:
        dimension = 2
        metric = 'l2'
        ntotal = 3

    fake = FakeIndex()
    loads = []
    barrier = threading.Barrier(2)
    cache = SnapshotCache(
        loader=lambda _path, _descriptor: (loads.append(True), fake)[1]
    )

    def lease_once():
        with cache.lease(revision) as handle:
            barrier.wait(timeout=5)
            return handle.index

    with ThreadPoolExecutor(max_workers=2) as executor:
        leased = tuple(executor.map(lambda _value: lease_once(), range(2)))

    assert leased == (fake, fake)
    assert len(loads) == 1
    assert cache.lease_count(revision) == 0
