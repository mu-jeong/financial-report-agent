"""Read-only, deterministic assessment of a trusted copied V1 installation."""

from __future__ import annotations

import hashlib
import pickle
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from src.retrieval.identity import canonical_json, sha256_text
from src.retrieval.vector_index import read_faiss_index_file


ASSESSMENT_SCHEMA_VERSION = 1
DEFAULT_ARTIFACT_PATHS = (
    "reports.db",
    "vector_db/index.faiss",
    "vector_db/index.pkl",
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ALLOWED_PICKLE_GLOBALS = frozenset(
    {
        ("langchain_community.docstore.in_memory", "InMemoryDocstore"),
        ("langchain_core.documents.base", "Document"),
    }
)


class AssessmentError(ValueError):
    """Raised when copied V1 evidence is untrusted, incomplete, or inconsistent."""


class _LegacyInMemoryDocstore:
    """Minimal pickle target for the retired LangChain community docstore.

    V1 persisted only the instance ``__dict__`` (including ``_dict``).  Mapping
    the historical global to this inert carrier keeps migration independent of
    the removed ``langchain-community`` runtime package.
    """


class _LegacyDocument:
    """Inert carrier for V1 ``Document`` instances during trusted import."""

    def __setstate__(self, state: Any) -> None:
        # Pydantic-backed LangChain releases wrap model fields in a nested
        # ``__dict__`` state, while older releases pickle the fields directly.
        # Retain only the plain field mapping needed by the migration reader.
        if isinstance(state, dict) and isinstance(state.get("__dict__"), dict):
            self.__dict__.update(state["__dict__"])
        elif isinstance(state, dict):
            self.__dict__.update(state)
        else:
            raise AssessmentError("legacy document pickle state must be a mapping")


@dataclass(frozen=True)
class ArtifactFingerprint:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ArtifactObservableEvidence:
    report_count: int
    embedded_report_count: int
    parent_count: int
    index_type: str
    dimension: int
    metric: str
    ntotal: int
    mapping_count: int
    docstore_count: int
    finite_vector_count: int
    vector_payload_sha256: str


@dataclass(frozen=True)
class ProvenanceEvidence:
    """Claims supplied independently of artifact-byte observation."""

    model: str | None = None
    model_revision: str | None = None
    normalization: str | None = None
    library_version: str | None = None
    same_space_attested: bool = False


@dataclass(frozen=True)
class V1Assessment:
    artifacts: tuple[ArtifactFingerprint, ...]
    observable: ArtifactObservableEvidence
    provenance: ProvenanceEvidence
    uncertainties: tuple[str, ...]
    schema_version: int = ASSESSMENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def digest(self) -> str:
        return sha256_text(self.canonical_json)


def assess_v1_install(
    install_root: str | Path,
    *,
    expected_hashes: Mapping[str, str],
    provenance: ProvenanceEvidence | None = None,
) -> V1Assessment:
    """Assess copied V1 artifacts without opening any path for writing.

    Deserialization occurs only after all three expected hashes, path
    containment, regular-file, non-symlink, and same-owner checks pass.  The
    restricted unpickler admits only the two classes used by LangChain's V1
    docstore.  The caller must still treat the copied bundle as locally trusted.
    """

    root = _trusted_root(install_root)
    normalized_expected = _normalize_expected_hashes(expected_hashes)
    paths = {
        relative_path: _trusted_artifact(root, relative_path)
        for relative_path in DEFAULT_ARTIFACT_PATHS
    }
    before = tuple(
        _fingerprint(paths[relative_path], relative_path)
        for relative_path in DEFAULT_ARTIFACT_PATHS
    )
    for fingerprint in before:
        if normalized_expected[fingerprint.relative_path] != fingerprint.sha256:
            raise AssessmentError(
                f"trusted hash mismatch for {fingerprint.relative_path}"
            )

    database = _assess_database(paths["reports.db"])
    index = _load_legacy_index(paths["vector_db/index.faiss"])
    docstore, mapping = load_trusted_legacy_docstore(paths["vector_db/index.pkl"])
    mapping_count, docstore_count = _validate_docstore_mapping(docstore, mapping)

    ntotal = int(index.ntotal)
    if ntotal != mapping_count or ntotal != docstore_count:
        raise AssessmentError(
            "legacy mapping count, docstore count, and FAISS ntotal must be equal"
        )
    _validate_docstore_references(
        docstore,
        mapping,
        report_names=database["report_names"],
        parent_ids=database["parent_ids"],
    )
    metric = _legacy_metric(index)
    finite_count, vector_hash = _hash_and_validate_vectors(index, metric)

    after = tuple(
        _fingerprint(paths[relative_path], relative_path)
        for relative_path in DEFAULT_ARTIFACT_PATHS
    )
    if before != after:
        raise AssessmentError("V1 artifacts changed during read-only assessment")

    supplied_provenance = provenance or ProvenanceEvidence()
    uncertainties = _provenance_uncertainties(supplied_provenance)
    observable = ArtifactObservableEvidence(
        report_count=database["report_count"],
        embedded_report_count=database["embedded_report_count"],
        parent_count=database["parent_count"],
        index_type=type(index).__name__,
        dimension=int(index.d),
        metric=metric,
        ntotal=ntotal,
        mapping_count=mapping_count,
        docstore_count=docstore_count,
        finite_vector_count=finite_count,
        vector_payload_sha256=vector_hash,
    )
    return V1Assessment(
        artifacts=before,
        observable=observable,
        provenance=supplied_provenance,
        uncertainties=uncertainties,
    )


def _trusted_root(value: str | Path) -> Path:
    lexical = Path(value).absolute()
    if not lexical.is_dir():
        raise AssessmentError("copied V1 install root is missing")
    if lexical.is_symlink():
        raise AssessmentError("copied V1 install root cannot be a symlink")
    resolved = lexical.resolve(strict=True)
    if resolved != lexical.resolve():
        raise AssessmentError("copied V1 install root cannot traverse a symlink")
    return resolved


def _trusted_artifact(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise AssessmentError("artifact path must remain relative to the copied root")
    lexical = root.joinpath(relative)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AssessmentError(f"artifact path cannot contain a symlink: {relative_path}")
    if not lexical.is_file():
        raise AssessmentError(f"required V1 artifact is missing: {relative_path}")
    resolved = lexical.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AssessmentError("artifact resolves outside the copied install root") from exc
    root_uid = getattr(root.stat(), "st_uid", None)
    artifact_uid = getattr(resolved.stat(), "st_uid", None)
    if root_uid is not None and artifact_uid is not None and root_uid != artifact_uid:
        raise AssessmentError(f"artifact owner differs from copied root: {relative_path}")
    return resolved


def _normalize_expected_hashes(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise AssessmentError("expected hashes must be a mapping")
    if set(values) != set(DEFAULT_ARTIFACT_PATHS):
        raise AssessmentError("expected hashes must cover exactly the three V1 artifacts")
    result: dict[str, str] = {}
    for relative_path in DEFAULT_ARTIFACT_PATHS:
        digest = values[relative_path]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise AssessmentError(f"invalid trusted SHA-256 for {relative_path}")
        result[relative_path] = digest.lower()
    return result


def _fingerprint(path: Path, relative_path: str) -> ArtifactFingerprint:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return ArtifactFingerprint(
        relative_path=relative_path,
        size_bytes=path.stat().st_size,
        sha256=digest.hexdigest(),
    )


def _assess_database(path: Path) -> dict[str, Any]:
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise AssessmentError("legacy SQLite quick_check failed")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise AssessmentError("legacy SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AssessmentError("legacy SQLite foreign-key check failed")

        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"reports", "parent_chunks"} <= tables:
            raise AssessmentError("legacy SQLite schema is missing required V1 tables")
        report_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(reports)")
        }
        required_report_columns = {
            "report_type",
            "report_date",
            "target_name",
            "title",
            "broker",
            "file_name",
            "is_embedded",
        }
        if not required_report_columns <= report_columns:
            raise AssessmentError("legacy reports table is missing required columns")
        parent_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(parent_chunks)")
        }
        if not {"id", "content", "file_name"} <= parent_columns:
            raise AssessmentError("legacy parent_chunks table is missing required columns")

        report_rows = connection.execute(
            "SELECT file_name, is_embedded FROM reports"
        ).fetchall()
        parent_rows = connection.execute("SELECT id FROM parent_chunks").fetchall()
        report_names = {row[0] for row in report_rows}
        parent_ids = {row[0] for row in parent_rows}
        if len(report_names) != len(report_rows):
            raise AssessmentError("legacy reports contain duplicate file_name values")
        if len(parent_ids) != len(parent_rows):
            raise AssessmentError("legacy parent_chunks contain duplicate IDs")
        return {
            "report_count": len(report_rows),
            "embedded_report_count": sum(int(row[1] == 1) for row in report_rows),
            "parent_count": len(parent_rows),
            "report_names": report_names,
            "parent_ids": parent_ids,
        }
    except sqlite3.Error as exc:
        raise AssessmentError(f"legacy SQLite assessment failed: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _load_legacy_index(path: Path) -> faiss.Index:
    try:
        index = read_faiss_index_file(path)
    except (RuntimeError, ValueError) as exc:
        raise AssessmentError(f"legacy FAISS index cannot be read: {exc}") from exc
    if int(index.d) <= 0:
        raise AssessmentError("legacy FAISS dimension must be positive")
    if int(index.ntotal) <= 0:
        raise AssessmentError("legacy FAISS index must contain vectors")
    _legacy_metric(index)
    return index


class _RestrictedLegacyUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if (module, name) not in _ALLOWED_PICKLE_GLOBALS:
            raise AssessmentError(
                f"legacy pickle references a forbidden global: {module}.{name}"
            )
        if name == "InMemoryDocstore":
            return _LegacyInMemoryDocstore
        if name == "Document":
            return _LegacyDocument
        raise AssertionError("allowed legacy pickle global is not mapped")


def load_trusted_legacy_docstore(path: str | Path) -> tuple[Any, Any]:
    source = Path(path)
    try:
        with source.open("rb") as stream:
            value = _RestrictedLegacyUnpickler(stream).load()
    except AssessmentError:
        raise
    except (OSError, pickle.PickleError, AttributeError, EOFError, ImportError) as exc:
        raise AssessmentError(f"trusted legacy pickle cannot be loaded: {exc}") from exc
    if not isinstance(value, tuple) or len(value) != 2:
        raise AssessmentError("legacy pickle must contain docstore and ordinal mapping")
    return value


def _validate_docstore_mapping(docstore: Any, mapping: Any) -> tuple[int, int]:
    raw_documents = getattr(docstore, "_dict", None)
    if not isinstance(raw_documents, dict) or not isinstance(mapping, dict):
        raise AssessmentError("legacy docstore and ordinal mapping must be dictionaries")
    if any(isinstance(key, bool) or not isinstance(key, int) for key in mapping):
        raise AssessmentError("legacy ordinal mapping keys must be integers")
    expected_ordinals = set(range(len(mapping)))
    if set(mapping) != expected_ordinals:
        raise AssessmentError("legacy ordinal mapping must cover exactly 0..N-1")
    document_ids = list(mapping.values())
    if any(not isinstance(value, str) or not value for value in document_ids):
        raise AssessmentError("legacy document IDs must be non-empty strings")
    if len(set(document_ids)) != len(document_ids):
        raise AssessmentError("legacy ordinal mapping contains duplicate document IDs")
    if set(document_ids) != set(raw_documents):
        raise AssessmentError("legacy mapping and docstore key sets differ")
    return len(mapping), len(raw_documents)


def _validate_docstore_references(
    docstore: Any,
    mapping: dict[int, str],
    *,
    report_names: set[str],
    parent_ids: set[str],
) -> None:
    documents = docstore._dict
    for ordinal in range(len(mapping)):
        document = documents[mapping[ordinal]]
        page_content = getattr(document, "page_content", None)
        metadata = getattr(document, "metadata", None)
        if not isinstance(page_content, str) or not page_content:
            raise AssessmentError(f"legacy document {ordinal} has no embedding text")
        if not isinstance(metadata, dict):
            raise AssessmentError(f"legacy document {ordinal} has invalid metadata")
        parent_id = metadata.get("parent_id")
        file_name = metadata.get("file_name")
        child_order = metadata.get("child_index")
        if parent_id not in parent_ids:
            raise AssessmentError(f"legacy document {ordinal} references a missing parent")
        if file_name not in report_names:
            raise AssessmentError(f"legacy document {ordinal} references a missing report")
        if (
            isinstance(child_order, bool)
            or not isinstance(child_order, int)
            or child_order < 0
        ):
            raise AssessmentError(f"legacy document {ordinal} has invalid child order")


def _legacy_metric(index: faiss.Index) -> str:
    metric_type = int(index.metric_type)
    if metric_type == faiss.METRIC_L2:
        return "l2"
    if metric_type == faiss.METRIC_INNER_PRODUCT:
        return "inner_product"
    raise AssessmentError(f"unsupported legacy FAISS metric: {metric_type}")


def _hash_and_validate_vectors(index: faiss.Index, metric: str) -> tuple[int, str]:
    ntotal = int(index.ntotal)
    dimension = int(index.d)
    digest = hashlib.sha256()
    for field in ("v1-vector-payload-v1", metric, str(dimension), str(ntotal)):
        encoded = field.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    finite_count = 0
    batch_size = max(1, min(4096, ntotal))
    for start in range(0, ntotal, batch_size):
        count = min(batch_size, ntotal - start)
        try:
            vectors = np.asarray(index.reconstruct_n(start, count), dtype=np.float32)
        except RuntimeError as exc:
            raise AssessmentError(f"legacy vectors cannot be reconstructed: {exc}") from exc
        if vectors.shape != (count, dimension):
            raise AssessmentError("legacy vector reconstruction returned the wrong shape")
        finite_rows = np.isfinite(vectors).all(axis=1)
        finite_count += int(finite_rows.sum())
        if not finite_rows.all():
            raise AssessmentError("legacy FAISS contains a non-finite vector")
        canonical_bytes = np.ascontiguousarray(vectors, dtype="<f4").tobytes(order="C")
        digest.update(canonical_bytes)
    return finite_count, digest.hexdigest()


def _provenance_uncertainties(value: ProvenanceEvidence) -> tuple[str, ...]:
    uncertainties: list[str] = []
    if not value.model:
        uncertainties.append("historical embedding model is not independently recorded")
    if not value.model_revision:
        uncertainties.append("historical embedding model revision is unknown")
    if not value.normalization:
        uncertainties.append("historical vector normalization is unknown")
    if not value.library_version:
        uncertainties.append("historical embedding library version is unknown")
    if not value.same_space_attested:
        uncertainties.append("same embedding space has not been attested")
    return tuple(uncertainties)


__all__ = [
    "ASSESSMENT_SCHEMA_VERSION",
    "ArtifactFingerprint",
    "ArtifactObservableEvidence",
    "AssessmentError",
    "DEFAULT_ARTIFACT_PATHS",
    "ProvenanceEvidence",
    "V1Assessment",
    "assess_v1_install",
    "load_trusted_legacy_docstore",
]
