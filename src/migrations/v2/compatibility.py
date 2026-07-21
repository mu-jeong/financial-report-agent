"""Isolated epoch-zero V1 compatibility reader.

This is the sole runtime trust-boundary exception that may deserialize a sealed
``index.pkl``.  Construction checks the irreversible runtime floor before the
bundle path is resolved or any pickle byte is opened.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from src.migrations.v2.assess import load_trusted_legacy_docstore
from src.migrations.v2.evidence import validate_compatibility_bundle
from src.retrieval.bootstrap import (
    RetrievalBootstrapError,
    inspect_runtime,
    resolve_epoch_zero_compatibility_bundle_id,
)
from src.retrieval.vector_index import read_faiss_index_file


class CompatibilityReaderError(RuntimeError):
    """Raised when V1 fallback is closed or its sealed bundle is unusable."""


@dataclass(frozen=True)
class LegacySearchResult:
    legacy_ordinal: int
    score: float
    embedding_text: str
    metadata: dict[str, Any]


class V1CompatibilityReader:
    """Search a hash-validated sealed V1 bundle only while epoch zero is open."""

    def __init__(
        self,
        data_root: str | Path,
        bundle_id: str,
    ) -> None:
        root = Path(data_root).resolve(strict=True)
        try:
            selection = inspect_runtime(
                root / "reports.db",
                data_root=root,
                validate_snapshot=False,
            )
        except (OSError, RetrievalBootstrapError) as exc:
            raise CompatibilityReaderError(
                "authoritative native runtime cannot authorize V1 compatibility"
            ) from exc
        if (
            not selection.is_native
            or selection.write_epoch != 0
            or not selection.v1_fallback_open
        ):
            raise CompatibilityReaderError(
                "V1 compatibility fallback is permanently closed"
            )
        try:
            authorized_bundle_id = resolve_epoch_zero_compatibility_bundle_id(
                selection
            )
        except RetrievalBootstrapError as exc:
            raise CompatibilityReaderError(
                "authoritative V1 compatibility evidence is unavailable"
            ) from exc
        if bundle_id != authorized_bundle_id:
            raise CompatibilityReaderError(
                "requested V1 bundle is not authorized by the active native snapshot"
            )
        manifest = validate_compatibility_bundle(root, bundle_id)
        bundle = root / "retrieval" / "compat" / "v1" / manifest.bundle_id
        try:
            index = read_faiss_index_file(bundle / "index.faiss")
            docstore, mapping = load_trusted_legacy_docstore(bundle / "index.pkl")
        except Exception as exc:
            raise CompatibilityReaderError(f"sealed V1 bundle cannot be opened: {exc}") from exc
        if not isinstance(mapping, dict) or set(mapping) != set(range(len(mapping))):
            raise CompatibilityReaderError("sealed V1 ordinal mapping is invalid")
        documents = getattr(docstore, "_dict", None)
        if not isinstance(documents, dict) or set(mapping.values()) != set(documents):
            raise CompatibilityReaderError("sealed V1 docstore mapping is inconsistent")
        if int(index.ntotal) != len(mapping):
            raise CompatibilityReaderError("sealed V1 FAISS and docstore counts differ")
        if int(index.metric_type) not in {
            faiss.METRIC_L2,
            faiss.METRIC_INNER_PRODUCT,
        }:
            raise CompatibilityReaderError("sealed V1 FAISS metric is unsupported")

        self._index: faiss.Index | None = index
        self._documents: dict[str, Any] | None = documents
        self._mapping: dict[int, str] | None = mapping
        self.bundle_id = manifest.bundle_id
        self.dimension = int(index.d)
        self.metric = (
            "l2" if int(index.metric_type) == faiss.METRIC_L2 else "inner_product"
        )

    @property
    def ntotal(self) -> int:
        self._require_open()
        assert self._index is not None
        return int(self._index.ntotal)

    def search(
        self,
        query_vector: np.ndarray,
        *,
        k: int,
        fetch_k: int | None = None,
        metadata_predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> list[LegacySearchResult]:
        self._require_open()
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            raise CompatibilityReaderError("k must be a positive integer")
        candidate_count = fetch_k if fetch_k is not None else k
        if (
            isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < k
        ):
            raise CompatibilityReaderError("fetch_k must be an integer greater than or equal to k")
        query = np.asarray(query_vector, dtype=np.float32)
        if query.ndim == 1:
            query = query.reshape(1, -1)
        if query.shape != (1, self.dimension) or not np.isfinite(query).all():
            raise CompatibilityReaderError(
                f"query vector must be one finite vector of dimension {self.dimension}"
            )
        assert self._index is not None
        assert self._mapping is not None
        assert self._documents is not None
        scores, ordinals = self._index.search(
            np.ascontiguousarray(query), min(candidate_count, self.ntotal)
        )
        results: list[LegacySearchResult] = []
        for ordinal, score in zip(ordinals[0], scores[0]):
            ordinal_value = int(ordinal)
            if ordinal_value < 0 or not np.isfinite(score):
                continue
            document = self._documents[self._mapping[ordinal_value]]
            metadata = dict(document.metadata)
            if metadata_predicate is not None and not metadata_predicate(metadata):
                continue
            results.append(
                LegacySearchResult(
                    legacy_ordinal=ordinal_value,
                    score=float(score),
                    embedding_text=document.page_content,
                    metadata=metadata,
                )
            )
            if len(results) == k:
                break
        return results

    def close(self) -> None:
        self._index = None
        self._documents = None
        self._mapping = None

    def __enter__(self) -> "V1CompatibilityReader":
        self._require_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._index is None:
            raise CompatibilityReaderError("V1 compatibility reader is closed")


__all__ = [
    "CompatibilityReaderError",
    "LegacySearchResult",
    "V1CompatibilityReader",
]
