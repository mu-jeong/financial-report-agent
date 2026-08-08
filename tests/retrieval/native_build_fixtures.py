from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from src.retrieval.build_service import (
    materialize_candidate,
    prepare_full_corpus_build,
    publish_candidate,
)
from src.retrieval.identity import EmbeddingProfile
from src.retrieval.initializer import initialize_empty_native
from src.retrieval.writer_lock import NativeWriterLock


PREFIX = "[Company: {target_name}, Title: {title}]\n"
VECTORS = np.asarray([[1.0, 0.0, 0.5], [0.0, 1.0, 0.25]], dtype=np.float32)


class DeterministicEmbeddings:
    def __init__(self, *, break_canary: bool = False):
        self.break_canary = break_canary
        self.calls: list[list[str]] = []
        self.canary_vectors = {
            PREFIX.format(target_name="A", title="Result") + "alpha": VECTORS[0],
            PREFIX.format(target_name="A", title="Result") + "beta": VECTORS[1],
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        values = []
        for text in texts:
            if text in self.canary_vectors:
                vector = self.canary_vectors[text].copy()
                if self.break_canary:
                    vector = -vector
            else:
                digest = hashlib.sha256(text.encode("utf-8")).digest()
                vector = np.asarray(
                    [1.0 + digest[0] / 255, 1.0 + digest[1] / 255, 1.0 + digest[2] / 255],
                    dtype=np.float32,
                )
            values.append(vector.tolist())
        return values


def _native_profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        model="model-a",
        dimension=3,
        metric="l2",
        normalization="none",
        prefix_template=PREFIX,
        extractor="deterministic-extractor",
        parent_policy={
            "algorithm": "langchain-recursive-v1",
            "chunk_size": 2000,
            "chunk_overlap": 200,
        },
        child_policy={
            "algorithm": "langchain-recursive-v1",
            "chunk_size": 500,
            "chunk_overlap": 50,
        },
    )


def _metadata(file_name: str):
    return {
        "a.pdf": {
            "report_type": "company",
            "report_date": "2026-01-01",
            "target_name": "A",
            "title": "Result",
            "broker": "Broker",
        },
        "b.pdf": {
            "report_type": "industry",
            "report_date": "2026-01-02",
            "target_name": "Sector",
            "title": "Outlook",
            "broker": "Broker",
        },
    }.get(file_name)


def _extract(path: Path, engine: str) -> str:
    assert engine == "deterministic-extractor"
    return {
        "a.pdf": "alpha beta current report content",
        "b.pdf": "sector outlook newly searchable content",
    }[path.name]


def _prepare(
    data_root: Path,
    sources: Path,
    embeddings: DeterministicEmbeddings,
    **overrides,
):
    writer_lease = overrides.pop("writer_lease", None)
    options = {
        "model": "model-a",
        "extractor_name": "deterministic-extractor",
        "parent_chunk_size": 40,
        "child_chunk_size": 20,
        "extractor": _extract,
        "metadata_parser": _metadata,
        **overrides,
    }
    if writer_lease is None:
        with NativeWriterLock(data_root) as owned_lease:
            return prepare_full_corpus_build(
                data_root,
                sources,
                embeddings=embeddings,
                writer_lease=owned_lease,
                **options,
            )
    return prepare_full_corpus_build(
        data_root,
        sources,
        embeddings=embeddings,
        writer_lease=writer_lease,
        **options,
    )


def _native_seed(
    tmp_path: Path,
    *,
    seed_matches_current_source: bool = False,
    profile: EmbeddingProfile | None = None,
) -> tuple[Path, Path]:
    data_root = tmp_path / "native-data-root"
    data_root.mkdir()
    sources = data_root / "downloaded"
    sources.mkdir()
    (sources / "a.pdf").write_bytes(b"current-a")
    (sources / "b.pdf").write_bytes(b"new-b")

    with NativeWriterLock(data_root) as lease:
        initialize_empty_native(data_root, writer_lease=lease)

    overrides: dict[str, object] = {}
    if profile is not None:
        overrides.update(
            model=profile.model,
            extractor_name=profile.extractor,
            extractor=lambda path, _engine: _extract(
                path,
                "deterministic-extractor",
            ),
            parent_chunk_size=int(profile.parent_policy["chunk_size"]),
            child_chunk_size=int(profile.child_policy["chunk_size"]),
            metric=profile.metric,
            normalization=profile.normalization,
            prefix_template=profile.prefix_template,
            allow_extraction_fallback=False,
        )
    plan = _prepare(data_root, sources, DeterministicEmbeddings(), **overrides)
    publish_candidate(materialize_candidate(plan, data_root), data_root)
    if not seed_matches_current_source:
        (sources / "a.pdf").write_bytes(b"changed-current-a")
    return data_root, sources
