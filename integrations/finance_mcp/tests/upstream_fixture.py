"""Small real Native V2 corpus used only by the integration contract tests."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from src.configs.config import EMBEDDING_MODEL
from src.retrieval import bootstrap as retrieval_bootstrap
from src.retrieval.delta_schema import install_delta_schema
from src.retrieval.initializer import initialize_empty_native
from src.retrieval.schema import configure_catalog_storage, install_schema
from src.retrieval.vector_index import build_index
from src.retrieval.update_lock import RetrievalUpdateLock
from src.retrieval.writer_lock import NativeWriterLock


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NativeFixture:
    data_root: Path
    catalog: Path
    rows: tuple[dict[str, object], ...]


def create_empty_fixture(root: Path) -> Path:
    data_root = root / "empty-data"
    data_root.mkdir()
    with RetrievalUpdateLock(data_root):
        with NativeWriterLock(data_root) as lease:
            initialize_empty_native(data_root, writer_lease=lease)
    return data_root


def create_native_fixture(root: Path, *, split_first_report: bool = False) -> NativeFixture:
    data_root = root / "data"
    v2_root = data_root / "retrieval" / "v2"
    v2_root.mkdir(parents=True)
    rows = (
        {
            "report_type": "company",
            "report_date": "2026-07-01",
            "target_name": "Alpha",
            "title": "Alpha first",
            "broker": "Broker A",
            "path": "reports/alpha-first.pdf",
            "body": "alpha first indexed body",
            "vector": (0.0, 5.0),
        },
        {
            "report_type": "company",
            "report_date": "2026-07-02",
            "target_name": "Beta",
            "title": "Beta report",
            "broker": "Broker B",
            "path": "reports/beta.pdf",
            "body": "beta indexed body",
            "vector": (5.0, 0.0),
        },
        {
            "report_type": "industry",
            "report_date": "2026-07-03",
            "target_name": "Alpha",
            "title": "Alpha industry",
            "broker": "Broker A",
            "path": "reports/alpha-industry.pdf",
            "body": "alpha industry indexed body",
            "vector": (2.5, 2.5),
        },
    )
    chunks: list[dict[str, object]] = []
    for report_id, row in enumerate(rows, 1):
        contents = (
            ("alpha first ", "indexed body")
            if split_first_report and report_id == 1
            else (str(row["body"]),)
        )
        for parent_order, content in enumerate(contents):
            suffix = "" if parent_order == 0 else f"-{parent_order}"
            chunks.append(
                {
                    "report_id": report_id,
                    "parent_order": parent_order,
                    "parent_uid": digest(f"parent-{report_id}{suffix}"),
                    "chunk_uid": digest(f"chunk-{report_id}{suffix}"),
                    "content": content,
                    "vector": row["vector"],
                }
            )
    descriptor = build_index(
        np.asarray([chunk["vector"] for chunk in chunks], dtype=np.float32),
        range(1, len(chunks) + 1),
        metric="l2",
    ).write(v2_root / "snapshots" / "snapshot-1.faiss")
    catalog = v2_root / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    install_schema(connection)
    install_delta_schema(connection)
    connection.execute(
        """
        INSERT INTO embedding_profiles (
            profile_id, profile_hash, model, dimension, metric, normalization,
            prefix_template, extractor, parent_policy_json, child_policy_json
        ) VALUES (?, ?, ?, 2, 'l2', 0, '', 'fixture', '{}', '{}')
        """,
        ("profile-1", digest("profile-1"), EMBEDDING_MODEL),
    )
    for report_id, row in enumerate(rows, 1):
        report_uid = digest(f"report-{report_id}")
        connection.execute(
            """
            INSERT INTO reports (
                report_id, report_uid, canonical_relative_path, source_sha256,
                retrieval_metadata_sha256, report_type, report_date,
                target_name, title, broker
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_id,
                report_uid,
                row["path"],
                digest(f"source-{report_id}"),
                digest(f"metadata-{report_id}"),
                row["report_type"],
                row["report_date"],
                row["target_name"],
                row["title"],
                row["broker"],
            ),
        )
    for chunk in chunks:
        content = str(chunk["content"])
        connection.execute(
            """
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order, content,
                content_sha256
            ) VALUES (?, ?, 'profile-1', ?, ?, ?)
            """,
            (
                chunk["parent_uid"],
                chunk["report_id"],
                chunk["parent_order"],
                content,
                digest(content),
            ),
        )
        connection.execute(
            """
            INSERT INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order, span_start,
                span_end, embedding_text_sha256
            ) VALUES (?, ?, 'profile-1', 0, 0, ?, ?)
            """,
            (chunk["chunk_uid"], chunk["parent_uid"], len(content), digest(content)),
        )
    connection.execute(
        """
        INSERT INTO retrieval_builds (
            build_id, profile_id, source_manifest_json, source_manifest_sha256,
            included_count, excluded_count, expected_count,
            exclusion_policy_version
        ) VALUES ('build-1', 'profile-1', '{}', ?, 3, 0, 3, 'fixture-v1')
        """,
        (digest("manifest"),),
    )
    connection.execute(
        """
        INSERT INTO vector_snapshots (
            snapshot_id, build_id, relative_path, file_sha256, size_bytes,
            dimension, metric, ntotal
        ) VALUES ('snapshot-1', 'build-1', ?, ?, ?, ?, ?, ?)
        """,
        (
            "retrieval/v2/snapshots/snapshot-1.faiss",
            descriptor.sha256,
            descriptor.size_bytes,
            descriptor.dimension,
            descriptor.metric,
            descriptor.ntotal,
        ),
    )
    for physical_id, chunk in enumerate(chunks, 1):
        connection.execute(
            "INSERT INTO snapshot_membership VALUES ('snapshot-1', ?, ?)",
            (chunk["chunk_uid"], physical_id),
        )
    for state in ("cataloging", "vector_building", "validating"):
        connection.execute(
            "UPDATE retrieval_builds SET state = ? WHERE build_id = 'build-1'",
            (state,),
        )
    for state in ("validating", "ready"):
        connection.execute(
            "UPDATE vector_snapshots SET state = ? WHERE snapshot_id = 'snapshot-1'",
            (state,),
        )
    for state in ("ready", "committed_pending_checkpoint", "fully_complete"):
        connection.execute(
            "UPDATE retrieval_builds SET state = ? WHERE build_id = 'build-1'",
            (state,),
        )
    connection.execute(
        """
        UPDATE retrieval_runtime
        SET active_snapshot_id = 'snapshot-1', active_build_id = 'build-1',
            publication_generation = 0
        WHERE runtime_id = 1
        """
    )
    connection.commit()
    connection.close()
    return NativeFixture(data_root, catalog, rows)


def publish_replacement(
    fixture: NativeFixture,
    *,
    path: str,
    body: str,
    sequence: int = 1,
) -> str:
    segment_id = digest(f"segment-{sequence}-{path}-{body}")
    report_uid = digest(f"delta-report-{sequence}-{path}-{body}")
    parent_uid = digest(f"delta-parent-{report_uid}")
    chunk_uid = digest(f"delta-chunk-{report_uid}")
    relative_path = f"retrieval/v2/deltas/{segment_id}.faiss"
    descriptor = build_index(
        np.asarray([[0.0, 5.0]], dtype=np.float32),
        (1,),
        metric="l2",
    ).write(fixture.data_root / relative_path)

    connection = sqlite3.connect(fixture.catalog)
    configure_catalog_storage(connection, writable=True)
    connection.execute("PRAGMA foreign_keys = ON")
    next_id = int(connection.execute("SELECT max(report_id) + 1 FROM reports").fetchone()[0])
    connection.execute(
        """
        INSERT INTO reports (
            report_id, report_uid, canonical_relative_path, source_sha256,
            retrieval_metadata_sha256, report_type, report_date,
            target_name, title, broker
        ) VALUES (?, ?, ?, ?, ?, 'company', '2026-08-01',
                  'Alpha', 'Alpha replacement', 'Broker A')
        """,
        (next_id, report_uid, path, digest(f"source-{report_uid}"), digest(f"meta-{report_uid}")),
    )
    connection.execute(
        "INSERT INTO retrieval_parents VALUES (?, ?, 'profile-1', 0, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
        (parent_uid, next_id, body, digest(body)),
    )
    connection.execute(
        "INSERT INTO retrieval_chunks VALUES (?, ?, 'profile-1', 0, 0, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
        (chunk_uid, parent_uid, len(body), digest(body)),
    )
    connection.execute(
        """
        INSERT INTO retrieval_delta_segments (
            segment_id, base_snapshot_id, base_publication_generation,
            sequence, relative_path, file_sha256, size_bytes, dimension,
            metric, ntotal
        ) VALUES (?, 'snapshot-1', 0, ?, ?, ?, ?, 2, 'l2', 1)
        """,
        (segment_id, sequence, relative_path, descriptor.sha256, descriptor.size_bytes),
    )
    connection.execute(
        "INSERT INTO retrieval_delta_reports (segment_id, canonical_relative_path, action, report_uid) VALUES (?, ?, 'upsert', ?)",
        (segment_id, path, report_uid),
    )
    connection.execute(
        "INSERT INTO retrieval_delta_membership (segment_id, chunk_uid, faiss_id) VALUES (?, ?, 1)",
        (segment_id, chunk_uid),
    )
    connection.execute(
        "UPDATE retrieval_delta_segments SET state = 'ready' WHERE segment_id = ?",
        (segment_id,),
    )
    connection.commit()
    connection.close()
    return report_uid


def publish_delete(
    fixture: NativeFixture,
    *,
    path: str,
    sequence: int,
) -> None:
    segment_id = digest(f"segment-{sequence}-delete-{path}")
    connection = sqlite3.connect(fixture.catalog)
    configure_catalog_storage(connection, writable=True)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        INSERT INTO retrieval_delta_segments (
            segment_id, base_snapshot_id, base_publication_generation,
            sequence, relative_path, file_sha256, size_bytes, dimension,
            metric, ntotal
        ) VALUES (?, 'snapshot-1', 0, ?, NULL, NULL, 0, 2, 'l2', 0)
        """,
        (segment_id, sequence),
    )
    connection.execute(
        """
        INSERT INTO retrieval_delta_reports (
            segment_id, canonical_relative_path, action, report_uid
        ) VALUES (?, ?, 'delete', NULL)
        """,
        (segment_id, path),
    )
    connection.execute(
        "UPDATE retrieval_delta_segments SET state = 'ready' WHERE segment_id = ?",
        (segment_id,),
    )
    connection.commit()
    connection.close()


def insert_inactive_parent(fixture: NativeFixture, *, report_id: int, body: str) -> None:
    parent_uid = digest(f"inactive-parent-{report_id}-{body}")
    chunk_uid = digest(f"inactive-chunk-{report_id}-{body}")
    connection = sqlite3.connect(fixture.catalog)
    configure_catalog_storage(connection, writable=True)
    connection.execute(
        """
        INSERT INTO retrieval_parents (
            parent_uid, report_id, profile_id, parent_order, content,
            content_sha256
        ) VALUES (?, ?, 'profile-1', 1, ?, ?)
        """,
        (parent_uid, report_id, body, digest(body)),
    )
    connection.execute(
        """
        INSERT INTO retrieval_chunks (
            chunk_uid, parent_uid, profile_id, child_order, span_start,
            span_end, embedding_text_sha256
        ) VALUES (?, ?, 'profile-1', 0, 0, ?, ?)
        """,
        (chunk_uid, parent_uid, len(body), digest(body)),
    )
    connection.commit()
    connection.close()


def response_with_profile(response: object, profile_id: str):
    """Return an otherwise identical native response carrying another profile."""

    revision = replace(response.revision, profile_id=profile_id)
    return replace(response, revision=revision)


def forbid_snapshot_load(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("metadata reads must not load the vector snapshot")

    monkeypatch.setattr(retrieval_bootstrap, "load_index", forbidden)


class FakeEmbeddings:
    def embed_query(self, _query: str) -> list[float]:
        return [0.0, 5.0]
