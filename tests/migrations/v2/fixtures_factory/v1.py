"""Build the smallest useful, deterministic legacy retrieval install.

The fixture is generated under a caller-owned temporary directory.  Nothing in
this module reads ``tests/fixtures`` or live ``data`` paths, and no PDF needs to
exist: source hashes are explicit migration evidence rather than invented file
contents.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from langchain_core.documents import Document

from src.retrieval.identity import EmbeddingProfile
from tests.migrations.v2.legacy_pickle_fixture import InMemoryDocstore


PREFIX_TEMPLATE = "[Company: {target_name}, Title: {title}]\n"
ARTIFACT_PATHS = (
    "reports.db",
    "vector_db/index.faiss",
    "vector_db/index.pkl",
)

_REPORTS = (
    {
        "id": 1,
        "key": "company_january",
        "report_type": "company",
        "report_date": "2026-01-15",
        "target_name": "Alpha Corp",
        "title": "January Review",
        "broker": "Broker One",
        "file_name": "company_2026-01-15_Alpha Corp_Broker One_January Review.pdf",
        "is_embedded": 1,
    },
    {
        "id": 2,
        "key": "company_february",
        "report_type": "company",
        "report_date": "2026-02-15",
        "target_name": "Alpha Corp",
        "title": "February Review",
        "broker": "Broker Two",
        "file_name": "company_2026-02-15_Alpha Corp_Broker Two_February Review.pdf",
        "is_embedded": 1,
    },
    {
        "id": 3,
        "key": "industry",
        "report_type": "industry",
        "report_date": "2026-02-20",
        "target_name": "Semiconductors",
        "title": "Sector Outlook",
        "broker": "Broker Three",
        "file_name": "industry_2026-02-20_Semiconductors_Broker Three_Sector Outlook.pdf",
        "is_embedded": 1,
    },
    {
        "id": 4,
        "key": "excluded",
        "report_type": "economy",
        "report_date": "2026-02-25",
        "target_name": "Macro",
        "title": "Pending Outlook",
        "broker": "Broker Four",
        "file_name": "economy_2026-02-25_Macro_Broker Four_Pending Outlook.pdf",
        "is_embedded": 0,
    },
)

_PARENTS = (
    {
        "id": "legacy-parent-alpha-main",
        "report_key": "company_january",
        "content": "same--alpha",
    },
    {
        "id": "legacy-parent-alpha-extra",
        "report_key": "company_january",
        "content": "alpha-extra",
    },
    {
        "id": "legacy-parent-beta-main",
        "report_key": "company_february",
        "content": "same--beta",
    },
    {
        "id": "legacy-parent-industry",
        "report_key": "industry",
        "content": "sector-only",
    },
)

# Ordinal order is the V1 index_to_docstore_id contract.  The repeated ``same``
# body intentionally belongs to two distinct reports/parents.
_CHUNKS = (
    {"id": "legacy-doc-0", "parent_id": "legacy-parent-alpha-main", "body": "same", "child_index": 0},
    {"id": "legacy-doc-1", "parent_id": "legacy-parent-alpha-main", "body": "alpha", "child_index": 1},
    {"id": "legacy-doc-2", "parent_id": "legacy-parent-alpha-extra", "body": "alpha-extra", "child_index": 0},
    {"id": "legacy-doc-3", "parent_id": "legacy-parent-beta-main", "body": "same", "child_index": 0},
    {"id": "legacy-doc-4", "parent_id": "legacy-parent-beta-main", "body": "beta", "child_index": 1},
    {"id": "legacy-doc-5", "parent_id": "legacy-parent-industry", "body": "sector-only", "child_index": 0},
)


@dataclass(frozen=True)
class V1Fixture:
    root: Path
    artifact_hashes: dict[str, str]
    artifact_sizes: dict[str, int]
    documents_by_ordinal: tuple[Document, ...]
    known_vectors: np.ndarray
    file_names: dict[str, str]
    parent_contents: dict[str, str]
    source_hashes: dict[str, str]

    @property
    def symbolic_n(self) -> int:
        """Return N from the generated corpus rather than a captured machine count."""

        return len(self.documents_by_ordinal)

    @property
    def known_squared_l2(self) -> tuple[float, ...]:
        return tuple(float(np.dot(vector, vector)) for vector in self.known_vectors)

    def current_artifact_hashes(self) -> dict[str, str]:
        return {
            relative_path: _sha256(self.root / relative_path)
            for relative_path in ARTIFACT_PATHS
        }

    def embedding_profile(self) -> EmbeddingProfile:
        return EmbeddingProfile(
            model="deterministic-v1-fixture",
            dimension=int(self.known_vectors.shape[1]),
            metric="l2",
            normalization="none",
            prefix_template=PREFIX_TEMPLATE,
            extractor="legacy-v1-parent-content",
            parent_policy={"size": 2000, "overlap": 200},
            child_policy={"size": 500, "overlap": 50},
        )


def build_v1_fixture(root: str | Path) -> V1Fixture:
    """Generate a byte-reproducible V1 SQLite/FAISS/pickle install."""

    fixture_root = Path(root)
    fixture_root.mkdir(parents=True, exist_ok=True)
    if any((fixture_root / relative_path).exists() for relative_path in ARTIFACT_PATHS):
        raise FileExistsError("V1 fixture artifacts already exist")

    reports_by_key = {report["key"]: report for report in _REPORTS}
    parents_by_id = {parent["id"]: parent for parent in _PARENTS}
    file_names = {report["key"]: report["file_name"] for report in _REPORTS}
    parent_contents = {parent["id"]: parent["content"] for parent in _PARENTS}

    _write_catalog(fixture_root / "reports.db", reports_by_key)
    vector_root = fixture_root / "vector_db"
    vector_root.mkdir()

    documents: dict[str, Document] = {}
    mapping: dict[int, str] = {}
    ordered_documents: list[Document] = []
    for ordinal, chunk in enumerate(_CHUNKS):
        parent = parents_by_id[chunk["parent_id"]]
        report = reports_by_key[parent["report_key"]]
        metadata = {
            "parent_id": parent["id"],
            "child_index": chunk["child_index"],
            "file_name": report["file_name"],
            "target_name": report["target_name"],
            "title": report["title"],
            "report_date": report["report_date"],
            "report_type": report["report_type"],
            "broker": report["broker"],
        }
        document = Document(
            page_content=PREFIX_TEMPLATE.format(**report) + chunk["body"],
            metadata=metadata,
        )
        documents[chunk["id"]] = document
        mapping[ordinal] = chunk["id"]
        ordered_documents.append(document)

    # Binary-exact float32 values yield known squared-L2 distances from zero:
    # 0, 0.25, 1, 2.25, 4, 6.25.  The other dimensions make the profile
    # realistic without obscuring ordering.
    known_vectors = np.asarray(
        [[ordinal * 0.5, 0.0, 0.0] for ordinal in range(len(_CHUNKS))],
        dtype=np.float32,
    )
    index = faiss.IndexFlatL2(int(known_vectors.shape[1]))
    index.add(known_vectors)
    _write_faiss(index, vector_root / "index.faiss")
    with (vector_root / "index.pkl").open("wb") as stream:
        pickle.dump((InMemoryDocstore(documents), mapping), stream, protocol=4)

    artifact_hashes = {
        relative_path: _sha256(fixture_root / relative_path)
        for relative_path in ARTIFACT_PATHS
    }
    artifact_sizes = {
        relative_path: (fixture_root / relative_path).stat().st_size
        for relative_path in ARTIFACT_PATHS
    }
    source_hashes = {
        report["file_name"]: hashlib.sha256(
            f"fixture-source:{report['file_name']}".encode("utf-8")
        ).hexdigest()
        for report in _REPORTS
    }
    return V1Fixture(
        root=fixture_root,
        artifact_hashes=artifact_hashes,
        artifact_sizes=artifact_sizes,
        documents_by_ordinal=tuple(ordered_documents),
        known_vectors=known_vectors,
        file_names=file_names,
        parent_contents=parent_contents,
        source_hashes=source_hashes,
    )


def _write_catalog(path: Path, reports_by_key: dict[str, dict]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY,
                report_type TEXT NOT NULL,
                report_date TEXT NOT NULL,
                target_name TEXT,
                title TEXT NOT NULL,
                broker TEXT NOT NULL,
                file_name TEXT NOT NULL UNIQUE,
                is_embedded INTEGER NOT NULL
            );
            CREATE TABLE parent_chunks (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                file_name TEXT NOT NULL,
                metadata TEXT
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO reports (
                id, report_type, report_date, target_name, title, broker,
                file_name, is_embedded
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    report["id"],
                    report["report_type"],
                    report["report_date"],
                    report["target_name"],
                    report["title"],
                    report["broker"],
                    report["file_name"],
                    report["is_embedded"],
                )
                for report in _REPORTS
            ],
        )
        connection.executemany(
            "INSERT INTO parent_chunks (id, content, file_name, metadata) VALUES (?, ?, ?, ?)",
            [
                (
                    parent["id"],
                    parent["content"],
                    reports_by_key[parent["report_key"]]["file_name"],
                    json.dumps(
                        {
                            "file_name": reports_by_key[parent["report_key"]]["file_name"],
                            "parent_id": parent["id"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                for parent in _PARENTS
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_faiss(index: faiss.Index, path: Path) -> None:
    # The callback writer keeps this generated fixture usable under Unicode
    # Windows temp paths, where FAISS's narrow path API can fail.
    with path.open("wb") as stream:
        writer = faiss.PyCallbackIOWriter(stream.write)
        faiss.write_index(index, writer)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["ARTIFACT_PATHS", "PREFIX_TEMPLATE", "V1Fixture", "build_v1_fixture"]
