"""Full-N structural import planning for a trusted copied V1 corpus."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from src.migrations.v2.assess import (
    ProvenanceEvidence,
    V1Assessment,
    assess_v1_install,
    load_trusted_legacy_docstore,
)
from src.migrations.v2.reconstruct import (
    ReconstructedSpan,
    ReconstructionError,
    render_embedding_prefix,
    resolve_ordered_spans,
)
from src.retrieval.identity import canonical_json, sha256_text
from src.retrieval.vector_index import read_faiss_index_file


class LegacyImportError(ValueError):
    """Raised when any V1 row cannot enter the one-shape native conversion."""


_RETRIEVAL_METADATA_FIELDS = (
    "file_name",
    "report_type",
    "report_date",
    "target_name",
    "title",
    "broker",
)


@dataclass(frozen=True)
class LegacyChild:
    legacy_ordinal: int
    legacy_document_id: str
    child_order: int
    embedding_text: str
    span: ReconstructedSpan


@dataclass(frozen=True)
class LegacyParent:
    legacy_parent_id: str
    file_name: str
    content: str
    content_sha256: str
    canonical_order_key: str
    vector_payload_sha256: str
    children: tuple[LegacyChild, ...]


@dataclass(frozen=True)
class LegacyReconstruction:
    assessment: V1Assessment
    parents: tuple[LegacyParent, ...]
    reconstruction_digest: str

    @property
    def parent_count(self) -> int:
        return len(self.parents)

    @property
    def chunk_count(self) -> int:
        return sum(len(parent.children) for parent in self.parents)


def reconstruct_v1_documents(
    copied_install_root: str | Path,
    *,
    expected_hashes: dict[str, str],
    prefix_template: str,
    provenance: ProvenanceEvidence | None = None,
) -> LegacyReconstruction:
    """Prove all V1 child spans without PDF, extraction, embedding, or network."""

    root = Path(copied_install_root).resolve(strict=True)
    before = assess_v1_install(
        root,
        expected_hashes=expected_hashes,
        provenance=provenance,
    )
    reports, parents = _read_legacy_catalog(root / "reports.db")
    docstore, mapping = load_trusted_legacy_docstore(root / "vector_db" / "index.pkl")
    documents = docstore._dict
    try:
        legacy_index = read_faiss_index_file(root / "vector_db" / "index.faiss")
    except (RuntimeError, ValueError) as exc:
        raise LegacyImportError(f"legacy FAISS cannot be reopened: {exc}") from exc

    grouped: dict[str, list[tuple[int, str, Any]]] = {}
    for legacy_ordinal in range(len(mapping)):
        document_id = mapping[legacy_ordinal]
        document = documents[document_id]
        parent_id = document.metadata["parent_id"]
        grouped.setdefault(parent_id, []).append((legacy_ordinal, document_id, document))

    converted: list[LegacyParent] = []
    for parent_id, parent_row in parents.items():
        raw_children = grouped.get(parent_id)
        if not raw_children:
            raise LegacyImportError(f"legacy parent has no vector-backed children: {parent_id}")
        file_name = parent_row["file_name"]
        report = reports.get(file_name)
        if report is None:
            raise LegacyImportError("legacy parent references a missing report")

        by_child_order: dict[int, tuple[int, str, Any]] = {}
        for raw_child in raw_children:
            child_order = raw_child[2].metadata.get("child_index")
            if child_order in by_child_order:
                raise LegacyImportError(
                    f"duplicate child order for legacy parent: {parent_id}"
                )
            by_child_order[child_order] = raw_child
            _validate_child_metadata(raw_child[2].metadata, report)
        if set(by_child_order) != set(range(len(by_child_order))):
            raise LegacyImportError(
                f"legacy child orders are not contiguous for parent: {parent_id}"
            )

        ordered = [by_child_order[index] for index in range(len(by_child_order))]
        prefix = render_embedding_prefix(prefix_template, report)
        embedding_texts = [item[2].page_content for item in ordered]
        try:
            spans = resolve_ordered_spans(
                parent_row["content"],
                embedding_texts,
                prefix,
            )
        except ReconstructionError as exc:
            raise LegacyImportError(
                f"legacy span reconstruction failed for parent {parent_id}: {exc}"
            ) from exc
        children = tuple(
            LegacyChild(
                legacy_ordinal=legacy_ordinal,
                legacy_document_id=document_id,
                child_order=child_order,
                embedding_text=document.page_content,
                span=spans[child_order],
            )
            for child_order, (legacy_ordinal, document_id, document) in enumerate(ordered)
        )
        vector_payload_sha256 = _parent_vector_payload_hash(legacy_index, children)
        content_sha256 = sha256_text(parent_row["content"])
        order_key = sha256_text(
            canonical_json(
                {
                    "content_sha256": content_sha256,
                    "children": [
                        {
                            "child_order": child.child_order,
                            "embedding_text_sha256": child.span.embedding_text_sha256,
                            "span_end": child.span.span_end,
                            "span_start": child.span.span_start,
                        }
                        for child in children
                    ],
                }
            )
        )
        converted.append(
            LegacyParent(
                legacy_parent_id=parent_id,
                file_name=file_name,
                content=parent_row["content"],
                content_sha256=content_sha256,
                canonical_order_key=order_key,
                vector_payload_sha256=vector_payload_sha256,
                children=children,
            )
        )

    if set(grouped) != set(parents):
        unknown = sorted(set(grouped) - set(parents))
        raise LegacyImportError(f"legacy vector references unknown parent: {unknown[0]}")
    if sum(len(parent.children) for parent in converted) != before.observable.ntotal:
        raise LegacyImportError("reconstructed child count does not equal legacy N")
    _validate_parent_order_keys(converted)

    converted.sort(
        key=lambda parent: (
            parent.file_name,
            parent.canonical_order_key,
            parent.vector_payload_sha256,
        )
    )
    digest = sha256_text(
        canonical_json(
            {
                "assessment_digest": before.digest,
                "parents": [
                    {
                        "canonical_order_key": parent.canonical_order_key,
                        "content_sha256": parent.content_sha256,
                        "file_name": parent.file_name,
                        "legacy_parent_id": parent.legacy_parent_id,
                        "vector_payload_sha256": parent.vector_payload_sha256,
                        "children": [
                            {
                                "child_order": child.child_order,
                                "embedding_text_sha256": child.span.embedding_text_sha256,
                                "legacy_document_id": child.legacy_document_id,
                                "legacy_ordinal": child.legacy_ordinal,
                                "span_end": child.span.span_end,
                                "span_start": child.span.span_start,
                            }
                            for child in parent.children
                        ],
                    }
                    for parent in converted
                ],
                "schema_version": 1,
            }
        )
    )
    after = assess_v1_install(
        root,
        expected_hashes=expected_hashes,
        provenance=provenance,
    )
    if before != after:
        raise LegacyImportError("copied V1 artifacts changed during reconstruction")
    return LegacyReconstruction(before, tuple(converted), digest)


def _read_legacy_catalog(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        report_rows = connection.execute(
            """
            SELECT file_name, report_type, report_date, target_name, title, broker
            FROM reports
            """
        ).fetchall()
        parent_rows = connection.execute(
            "SELECT id, content, file_name FROM parent_chunks"
        ).fetchall()
    except sqlite3.Error as exc:
        raise LegacyImportError(f"legacy catalog read failed: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    reports = {row["file_name"]: dict(row) for row in report_rows}
    parents = {row["id"]: dict(row) for row in parent_rows}
    if len(reports) != len(report_rows) or len(parents) != len(parent_rows):
        raise LegacyImportError("legacy catalog contains duplicate identities")
    return reports, parents


def _validate_child_metadata(
    metadata: dict[str, Any],
    report: dict[str, Any],
) -> None:
    for field in _RETRIEVAL_METADATA_FIELDS:
        if metadata.get(field) != report.get(field):
            raise LegacyImportError(f"legacy child/report metadata mismatch: {field}")


def _validate_parent_order_keys(parents: list[LegacyParent]) -> None:
    by_report: dict[tuple[str, str, str], LegacyParent] = {}
    for parent in parents:
        key = (
            parent.file_name,
            parent.canonical_order_key,
            parent.vector_payload_sha256,
        )
        existing = by_report.get(key)
        if existing is not None and (
            existing.content != parent.content
            or tuple(child.embedding_text for child in existing.children)
            != tuple(child.embedding_text for child in parent.children)
        ):
            raise LegacyImportError("canonical parent-order key collision")
        by_report[key] = parent


def _parent_vector_payload_hash(
    index: faiss.Index,
    children: tuple[LegacyChild, ...],
) -> str:
    digest = hashlib.sha256()
    for field in ("legacy-parent-vectors-v1", str(int(index.d)), str(len(children))):
        encoded = field.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    for child in children:
        try:
            vector = np.asarray(index.reconstruct(child.legacy_ordinal), dtype=np.float32)
        except RuntimeError as exc:
            raise LegacyImportError(
                f"legacy vector cannot be reconstructed at ordinal {child.legacy_ordinal}"
            ) from exc
        if vector.shape != (int(index.d),) or not np.isfinite(vector).all():
            raise LegacyImportError(
                f"legacy vector is invalid at ordinal {child.legacy_ordinal}"
            )
        digest.update(np.ascontiguousarray(vector, dtype="<f4").tobytes())
    return digest.hexdigest()


__all__ = [
    "LegacyChild",
    "LegacyImportError",
    "LegacyParent",
    "LegacyReconstruction",
    "reconstruct_v1_documents",
]
