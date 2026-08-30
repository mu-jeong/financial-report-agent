from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from src.core.fixed_snapshot import (
    DEFAULT_READER_CONTRACT,
    FixedSnapshotAvailability,
    FixedSnapshotError,
    create_fixed_snapshot,
    derive_fixed_snapshot_availability,
    list_active_report_documents,
    open_fixed_snapshot,
    propose_report_scope,
    propose_report_scope_from_documents,
    resolve_active_snapshot_sources,
    restore_fixed_snapshot,
)
from src.retrieval.delta_schema import install_delta_schema
from src.retrieval.schema import install_schema
from src.retrieval.vector_index import build_index


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _native_source(tmp_path: Path) -> tuple[Path, Path, tuple[str, str], tuple[str, str]]:
    root = tmp_path / "native"
    snapshot_path = root / "snapshots" / "active.faiss"
    descriptor = build_index(
        np.asarray([[1.0, 0.0], [0.8, 0.2], [0.0, 1.0]], dtype=np.float32),
        [1, 2, 3],
        "l2",
    ).write(snapshot_path)
    catalog_path = root / "catalog.sqlite3"
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(catalog_path)
    install_schema(connection)
    connection.execute(
        """
        INSERT INTO embedding_profiles (
            profile_id, profile_hash, model, dimension, metric, normalization,
            prefix_template, extractor, parent_policy_json, child_policy_json,
            created_at
        ) VALUES ('profile', ?, 'test-model', 2, 'l2', 0, '', 'test', '{}', '{}', ?)
        """,
        (_digest("profile"), "2026-08-29T00:00:00Z"),
    )
    report_uids = (_digest("report-a"), _digest("report-b"))
    chunk_uids = (_digest("chunk-a1"), _digest("chunk-a2"), _digest("chunk-b1"))
    physical_id = 0
    for report_index, (report_uid, chunk_count) in enumerate(
        zip(report_uids, (2, 1)), 1
    ):
        connection.execute(
            """
            INSERT INTO reports (
                report_id, report_uid, canonical_relative_path, source_sha256,
                retrieval_metadata_sha256, report_type, report_date,
                target_name, title, broker, created_at
            ) VALUES (?, ?, ?, ?, ?, 'company', '2026-08-29', ?, ?, 'Broker', ?)
            """,
            (
                report_index,
                report_uid,
                f"reports/{report_index}.pdf",
                _digest(f"source-{report_index}"),
                _digest(f"metadata-{report_index}"),
                f"Company {report_index}",
                f"Report {report_index}",
                "2026-08-29T00:00:00Z",
            ),
        )
        parent_uid = _digest(f"parent-{report_index}")
        connection.execute(
            """
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order, content,
                content_sha256, created_at
            ) VALUES (?, ?, 'profile', 0, ?, ?, ?)
            """,
            (
                parent_uid,
                report_index,
                "abcdefghij",
                _digest("abcdefghij"),
                "2026-08-29T00:00:00Z",
            ),
        )
        for child_order in range(chunk_count):
            physical_id += 1
            chunk_uid = chunk_uids[physical_id - 1]
            connection.execute(
                """
                INSERT INTO retrieval_chunks (
                    chunk_uid, parent_uid, profile_id, child_order, span_start,
                    span_end, embedding_text_sha256, created_at
                ) VALUES (?, ?, 'profile', ?, ?, ?, ?, ?)
                """,
                (
                    chunk_uid,
                    parent_uid,
                    child_order,
                    child_order * 3,
                    child_order * 3 + 3,
                    _digest(f"embedding-{physical_id}"),
                    "2026-08-29T00:00:00Z",
                ),
            )
    connection.execute(
        """
        INSERT INTO retrieval_builds (
            build_id, profile_id, source_manifest_json, source_manifest_sha256,
            included_count, excluded_count, expected_count,
            exclusion_policy_version, created_at, state_changed_at
        ) VALUES ('build', 'profile', '{}', ?, 3, 0, 3, 'test-v1', ?, ?)
        """,
        (_digest("manifest"), "2026-08-29T00:00:00Z", "2026-08-29T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO vector_snapshots (
            snapshot_id, build_id, relative_path, file_sha256, size_bytes,
            dimension, metric, ntotal, created_at, state_changed_at
        ) VALUES ('active', 'build', 'snapshots/active.faiss', ?, ?, 2, 'l2', 3, ?, ?)
        """,
        (
            descriptor.sha256,
            descriptor.size_bytes,
            "2026-08-29T00:00:00Z",
            "2026-08-29T00:00:00Z",
        ),
    )
    for faiss_id, chunk_uid in enumerate(chunk_uids, 1):
        connection.execute(
            "INSERT INTO snapshot_membership VALUES ('active', ?, ?)",
            (chunk_uid, faiss_id),
        )
    for state in ("cataloging", "vector_building", "validating"):
        connection.execute("UPDATE retrieval_builds SET state=?", (state,))
    for state in ("validating", "ready"):
        connection.execute("UPDATE vector_snapshots SET state=?", (state,))
    for state in ("ready", "committed_pending_checkpoint", "fully_complete"):
        connection.execute("UPDATE retrieval_builds SET state=?", (state,))
    connection.execute(
        """
        UPDATE retrieval_runtime
        SET active_snapshot_id='active', active_build_id='build',
            publication_generation=4
        WHERE runtime_id=1
        """
    )
    connection.commit()
    connection.close()
    return catalog_path, snapshot_path, report_uids, chunk_uids


def test_create_snapshot_projects_whole_report_and_opens_without_source(tmp_path: Path):
    catalog, source_index, report_uids, chunk_uids = _native_source(tmp_path)
    managed_root = tmp_path / "managed"

    created = create_fixed_snapshot(
        catalog,
        source_index,
        managed_root,
        chunk_uids=[chunk_uids[0]],
    )

    assert created.path.name == created.revision_id
    assert not any(path.name.startswith(".temp-") for path in managed_root.iterdir())
    opened = open_fixed_snapshot(managed_root, created.revision_id)
    assert opened.report_uids == (report_uids[0],)
    assert opened.chunk_uids == tuple(sorted(chunk_uids[:2]))
    assert opened.index.physical_ids == (1, 2)
    with sqlite3.connect(opened.catalog_path) as projected:
        assert projected.execute("SELECT count(*) FROM reports").fetchone()[0] == 1
        assert projected.execute("SELECT count(*) FROM retrieval_chunks").fetchone()[0] == 2
        assert projected.execute(
            "SELECT active_snapshot_id FROM retrieval_runtime WHERE runtime_id=1"
        ).fetchone()[0] == opened.manifest["projected_snapshot_id"]

    catalog.unlink()
    source_index.unlink()
    reopened = open_fixed_snapshot(managed_root, created.revision_id)
    assert reopened.index.ntotal == 2


def test_availability_detects_missing_corrupt_and_incompatible(tmp_path: Path):
    catalog, source_index, report_uids, _ = _native_source(tmp_path)
    managed_root = tmp_path / "managed"
    created = create_fixed_snapshot(
        catalog, source_index, managed_root, report_uids=[report_uids[0]]
    )
    assert derive_fixed_snapshot_availability(
        managed_root, created.revision_id
    ) is FixedSnapshotAvailability.AVAILABLE

    manifest_path = created.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["reader_contract"] = "future-reader-v99"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    assert derive_fixed_snapshot_availability(
        managed_root, created.revision_id
    ) is FixedSnapshotAvailability.INCOMPATIBLE

    manifest["reader_contract"] = DEFAULT_READER_CONTRACT
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    (created.path / "subset.faiss").write_bytes(b"corrupt")
    assert derive_fixed_snapshot_availability(
        managed_root, created.revision_id
    ) is FixedSnapshotAvailability.CORRUPT

    shutil.rmtree(created.path)
    assert derive_fixed_snapshot_availability(
        managed_root, created.revision_id
    ) is FixedSnapshotAvailability.LOCAL_MISSING


def test_exact_restore_keeps_revision_and_different_bytes_get_new_revision(tmp_path: Path):
    catalog, source_index, report_uids, _ = _native_source(tmp_path)
    managed_root = tmp_path / "managed"
    first = create_fixed_snapshot(
        catalog, source_index, managed_root, report_uids=[report_uids[0]]
    )
    repeated = create_fixed_snapshot(
        catalog, source_index, managed_root, report_uids=[report_uids[0]]
    )
    second = create_fixed_snapshot(
        catalog, source_index, managed_root, report_uids=[report_uids[1]]
    )
    assert repeated.revision_id == first.revision_id
    assert first.revision_id != second.revision_id

    backup = tmp_path / "backup"
    shutil.copytree(first.path, backup)
    shutil.rmtree(first.path)
    restored = restore_fixed_snapshot(backup, managed_root)
    assert restored.revision_id == first.revision_id
    assert derive_fixed_snapshot_availability(
        managed_root, first.revision_id
    ) is FixedSnapshotAvailability.AVAILABLE


def test_selection_and_managed_root_escape_fail_closed(tmp_path: Path):
    catalog, source_index, _, _ = _native_source(tmp_path)
    managed_root = tmp_path / "managed"
    with pytest.raises(FixedSnapshotError, match="exactly one"):
        create_fixed_snapshot(catalog, source_index, managed_root)
    with pytest.raises(FixedSnapshotError, match="not present"):
        create_fixed_snapshot(
            catalog,
            source_index,
            managed_root,
            report_uids=["f" * 64],
        )
    with pytest.raises(FixedSnapshotError, match="revision ID"):
        open_fixed_snapshot(managed_root, "../escape")


def test_open_rejects_valid_snapshot_under_a_different_revision_name(
    tmp_path: Path,
):
    catalog, source_index, report_uids, _ = _native_source(tmp_path)
    managed_root = tmp_path / "managed"
    source = create_fixed_snapshot(
        catalog,
        source_index,
        managed_root,
        report_uids=[report_uids[0]],
    )
    wrong_revision_id = "f" * 64
    assert wrong_revision_id != source.revision_id
    shutil.copytree(source.path, managed_root / wrong_revision_id)

    with pytest.raises(FixedSnapshotError, match="revision directory"):
        open_fixed_snapshot(managed_root, wrong_revision_id)


def test_open_rejects_managed_revision_directory_symlink(tmp_path: Path):
    catalog, source_index, report_uids, _ = _native_source(tmp_path)
    source = create_fixed_snapshot(
        catalog,
        source_index,
        tmp_path / "source-managed",
        report_uids=[report_uids[0]],
    )
    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    link = managed_root / source.revision_id
    try:
        link.symlink_to(source.path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(FixedSnapshotError, match="symbolic link"):
        open_fixed_snapshot(managed_root, source.revision_id)


@pytest.mark.parametrize(
    "filename",
    ("manifest.json", "projected_catalog.sqlite3", "subset.faiss"),
)
def test_open_rejects_symlinked_snapshot_files(
    tmp_path: Path,
    filename: str,
):
    catalog, source_index, report_uids, _ = _native_source(tmp_path)
    source = create_fixed_snapshot(
        catalog,
        source_index,
        tmp_path / "source-managed",
        report_uids=[report_uids[0]],
    )
    managed_root = tmp_path / "managed"
    target = managed_root / source.revision_id
    shutil.copytree(source.path, target)
    linked_file = target / filename
    linked_file.unlink()
    try:
        linked_file.symlink_to(source.path / filename)
    except OSError as exc:
        pytest.skip(f"file symlink creation is unavailable: {exc}")

    with pytest.raises(FixedSnapshotError, match="symbolic link"):
        open_fixed_snapshot(managed_root, source.revision_id)


def test_scope_proposal_keeps_observed_and_filter_competitors(tmp_path: Path):
    catalog, _, report_uids, _ = _native_source(tmp_path)

    proposal = propose_report_scope(
        catalog,
        observed_report_uids=[report_uids[0]],
        filters={"target_name": "Company 2", "unsupported_hint": "ignored"},
    )

    assert proposal.report_uids == tuple(sorted(report_uids))
    assert proposal.observed_report_uids == (report_uids[0],)
    assert proposal.filter_matched_report_uids == (report_uids[1],)
    assert proposal.unsupported_filters == ("unsupported_hint",)

    cached_documents = list_active_report_documents(catalog)
    cached_proposal = propose_report_scope_from_documents(
        cached_documents,
        observed_report_uids=[report_uids[0]],
        filters={"target_name": "Company 2", "unsupported_hint": "ignored"},
    )
    assert cached_proposal == proposal


def test_active_report_documents_expose_human_metadata_without_content(
    tmp_path: Path,
):
    catalog, _, report_uids, _ = _native_source(tmp_path)

    documents = list_active_report_documents(catalog)

    assert [document.report_uid for document in documents] == list(report_uids)
    assert documents[0].report_date == "2026-08-29"
    assert documents[0].target_name == "Company 1"
    assert documents[0].title == "Report 1"
    assert documents[0].broker == "Broker"
    assert documents[0].file_name == "1.pdf"
    assert not hasattr(documents[0], "content")


def test_active_snapshot_sources_are_resolved_below_data_root(tmp_path: Path):
    catalog, source_index, _, _ = _native_source(tmp_path / "source")
    data_root = tmp_path / "data"
    target_catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    target_index = data_root / "snapshots" / "active.faiss"
    target_catalog.parent.mkdir(parents=True)
    target_index.parent.mkdir(parents=True)
    shutil.copyfile(catalog, target_catalog)
    shutil.copyfile(source_index, target_index)

    resolved_catalog, resolved_index = resolve_active_snapshot_sources(data_root)

    assert resolved_catalog == target_catalog.resolve()
    assert resolved_index == target_index.resolve()


def test_scope_and_projection_include_effective_ready_delta_reports(tmp_path: Path):
    catalog, source_index, _, _ = _native_source(tmp_path)
    root = catalog.parent
    delta_report_uid = _digest("delta-report")
    delta_parent_uid = _digest("delta-parent")
    delta_chunk_uid = _digest("delta-chunk")
    delta_segment_id = _digest("delta-segment")
    delta_relative_path = f"deltas/{delta_segment_id}.faiss"
    delta_path = root / delta_relative_path
    delta_descriptor = build_index(
        np.asarray([[0.25, 0.75]], dtype=np.float32),
        [1],
        "l2",
    ).write(delta_path)

    connection = sqlite3.connect(catalog)
    install_delta_schema(connection)
    connection.execute(
        """
        INSERT INTO reports (
            report_uid, canonical_relative_path, source_sha256,
            retrieval_metadata_sha256, report_type, report_date,
            target_name, title, broker, created_at
        ) VALUES (?, 'reports/delta.pdf', ?, ?, 'company', '2026-08-29',
                  'Delta Company', 'Delta Report', 'Broker', ?)
        """,
        (
            delta_report_uid,
            _digest("delta-source"),
            _digest("delta-metadata"),
            "2026-08-29T00:00:00Z",
        ),
    )
    delta_report_id = connection.execute(
        "SELECT report_id FROM reports WHERE report_uid = ?",
        (delta_report_uid,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO retrieval_parents (
            parent_uid, report_id, profile_id, parent_order,
            content, content_sha256, created_at
        ) VALUES (?, ?, 'profile', 0, 'delta text', ?, ?)
        """,
        (
            delta_parent_uid,
            delta_report_id,
            _digest("delta text"),
            "2026-08-29T00:00:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO retrieval_chunks (
            chunk_uid, parent_uid, profile_id, child_order,
            span_start, span_end, embedding_text_sha256, created_at
        ) VALUES (?, ?, 'profile', 0, 0, 10, ?, ?)
        """,
        (
            delta_chunk_uid,
            delta_parent_uid,
            _digest("delta-embedding"),
            "2026-08-29T00:00:00Z",
        ),
    )
    connection.execute(
        """
        INSERT INTO retrieval_delta_segments (
            segment_id, base_snapshot_id, base_publication_generation,
            sequence, relative_path, file_sha256, size_bytes,
            dimension, metric, ntotal
        ) VALUES (?, 'active', 4, 1, ?, ?, ?, 2, 'l2', 1)
        """,
        (
            delta_segment_id,
            delta_relative_path,
            delta_descriptor.sha256,
            delta_descriptor.size_bytes,
        ),
    )
    connection.execute(
        """
        INSERT INTO retrieval_delta_reports (
            segment_id, canonical_relative_path, action, report_uid
        ) VALUES (?, 'reports/delta.pdf', 'upsert', ?)
        """,
        (delta_segment_id, delta_report_uid),
    )
    connection.execute(
        "INSERT INTO retrieval_delta_membership VALUES (?, ?, 1)",
        (delta_segment_id, delta_chunk_uid),
    )
    connection.execute(
        "UPDATE retrieval_delta_segments SET state = 'ready' WHERE segment_id = ?",
        (delta_segment_id,),
    )
    connection.commit()
    connection.close()

    proposal = propose_report_scope(
        catalog,
        observed_report_uids=[delta_report_uid],
    )
    assert proposal.report_uids == (delta_report_uid,)
    documents = list_active_report_documents(catalog)
    assert delta_report_uid in {
        document.report_uid for document in documents
    }
    assert next(
        document for document in documents
        if document.report_uid == delta_report_uid
    ).title == "Delta Report"

    created = create_fixed_snapshot(
        catalog,
        source_index,
        tmp_path / "managed",
        report_uids=[delta_report_uid],
    )
    opened = open_fixed_snapshot(tmp_path / "managed", created.revision_id)
    assert opened.report_uids == (delta_report_uid,)
    assert opened.chunk_uids == (delta_chunk_uid,)
    assert opened.manifest["source_artifacts"] == [
        {
            "artifact_id": delta_segment_id,
            "artifact_kind": "delta",
            "sequence": 1,
            "sha256": delta_descriptor.sha256,
            "size_bytes": delta_descriptor.size_bytes,
            "dimension": 2,
            "metric": "l2",
            "ntotal": 1,
        }
    ]
    np.testing.assert_allclose(
        opened.index.reconstruct([1]),
        np.asarray([[0.25, 0.75]], dtype=np.float32),
    )
