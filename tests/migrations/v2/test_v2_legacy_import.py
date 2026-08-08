from __future__ import annotations

import hashlib
import pickle
import sqlite3
from pathlib import Path

import faiss
import numpy as np
import pytest
from langchain_core.documents import Document

from src.migrations.v2.legacy_import import LegacyImportError, reconstruct_v1_documents
from tests.migrations.v2.legacy_pickle_fixture import InMemoryDocstore


PREFIX_TEMPLATE = "[Company: {target_name}, Title: {title}]\n"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(
    root: Path,
    *,
    ambiguous: bool = False,
    replayable_dot: bool = False,
) -> dict[str, str]:
    (root / "vector_db").mkdir(parents=True)
    if replayable_dot:
        content = "aaaaaaaaa. b.b.bbbbb. . ccccccccc."
        bodies = ["aaaaaaaaa", ". b.b.bbbbb", ".", ". ccccccccc."]
    else:
        content = "same--middle--same" if not ambiguous else "same--same--same"
        bodies = ["same", "middle", "same"] if not ambiguous else ["same", "same"]
    with sqlite3.connect(root / "reports.db") as connection:
        connection.executescript(
            f"""
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
            INSERT INTO reports VALUES
                (1, 'company', '2026-01-01', 'A', 'Result', 'Broker', 'a.pdf', 1);
            INSERT INTO parent_chunks VALUES ('p1', '{content}', 'a.pdf', NULL);
            """
        )
    documents = {}
    mapping = {}
    for index, body in enumerate(bodies):
        document_id = f"d{index}"
        mapping[index] = document_id
        documents[document_id] = Document(
            page_content=PREFIX_TEMPLATE.format(target_name="A", title="Result") + body,
            metadata={
                "parent_id": "p1",
                "file_name": "a.pdf",
                "report_type": "company",
                "report_date": "2026-01-01",
                "target_name": "A",
                "title": "Result",
                "broker": "Broker",
                "child_index": index,
            },
        )
    index = faiss.IndexFlatL2(2)
    index.add(np.asarray([[float(i), 1.0] for i in range(len(bodies))], dtype=np.float32))
    faiss.write_index(index, str(root / "vector_db" / "index.faiss"))
    with (root / "vector_db" / "index.pkl").open("wb") as stream:
        pickle.dump((InMemoryDocstore(documents), mapping), stream)
    return {
        "reports.db": _hash(root / "reports.db"),
        "vector_db/index.faiss": _hash(root / "vector_db" / "index.faiss"),
        "vector_db/index.pkl": _hash(root / "vector_db" / "index.pkl"),
    }


def test_full_n_reconstruction_is_unique_deterministic_and_overlap_safe(tmp_path):
    expected = _fixture(tmp_path)

    first = reconstruct_v1_documents(
        tmp_path,
        expected_hashes=expected,
        prefix_template=PREFIX_TEMPLATE,
    )
    second = reconstruct_v1_documents(
        tmp_path,
        expected_hashes=expected,
        prefix_template=PREFIX_TEMPLATE,
    )

    assert first.reconstruction_digest == second.reconstruction_digest
    assert first.parent_count == 1
    assert first.chunk_count == first.assessment.observable.ntotal == 3
    assert [child.span.span_start for child in first.parents[0].children] == [0, 6, 14]


def test_ambiguous_global_mapping_blocks_the_complete_import(tmp_path):
    expected = _fixture(tmp_path, ambiguous=True)

    with pytest.raises(LegacyImportError, match="multiple valid"):
        reconstruct_v1_documents(
            tmp_path,
            expected_hashes=expected,
            prefix_template=PREFIX_TEMPLATE,
        )


def test_ambiguous_dot_is_replayed_deterministically_from_the_frozen_policy(tmp_path):
    expected = _fixture(tmp_path, replayable_dot=True)
    child_policy = {
        "algorithm": "langchain-recursive-v1",
        "chunk_overlap": 1,
        "chunk_size": 12,
        "is_separator_regex": False,
        "keep_separator": True,
        "length_function": "python-len",
        "separators": ["\n\n", "\n", ". ", " ", ""],
        "strip_whitespace": True,
    }

    first = reconstruct_v1_documents(
        tmp_path,
        expected_hashes=expected,
        prefix_template=PREFIX_TEMPLATE,
        child_policy=child_policy,
    )
    second = reconstruct_v1_documents(
        tmp_path,
        expected_hashes=expected,
        prefix_template=PREFIX_TEMPLATE,
        child_policy=child_policy,
    )

    assert first.reconstruction_digest == second.reconstruction_digest
    assert [child.span.span_start for child in first.parents[0].children] == [0, 9, 20, 22]
    assert len(first.replay_claims) == 1
    assert first.replay_claims[0].ambiguous_child_order == 2
    assert first.replay_claims[0].selected_start == 20
    assert first.replay_claims[0].full_sequence_replay_matched is True


def test_metadata_mismatch_blocks_the_complete_import(tmp_path):
    expected = _fixture(tmp_path)
    pickle_path = tmp_path / "vector_db" / "index.pkl"
    with pickle_path.open("rb") as stream:
        docstore, mapping = pickle.load(stream)
    docstore._dict["d0"].metadata["broker"] = "Wrong"
    with pickle_path.open("wb") as stream:
        pickle.dump((docstore, mapping), stream)
    expected["vector_db/index.pkl"] = _hash(pickle_path)

    with pytest.raises(LegacyImportError, match="metadata mismatch"):
        reconstruct_v1_documents(
            tmp_path,
            expected_hashes=expected,
            prefix_template=PREFIX_TEMPLATE,
        )
