"""Process-scoped native retrieval dispatch without request-scoped state."""

from __future__ import annotations

import atexit
import threading
from dataclasses import dataclass
from pathlib import Path

from src.retrieval.bootstrap import (
    RetrievalPaths,
    RuntimeSelection,
    RuntimeValidationMode,
    inspect_runtime,
    retrieval_paths,
)
from src.retrieval.reader import NativeRetrievalReader
from src.retrieval.repository import CatalogRepository


class RetrievalDispatchStateError(RuntimeError):
    """Raised when one data root resolves to conflicting native catalogs."""


class NativeReaderHolder:
    """Own one reusable repository/reader pair and no request lease."""

    def __init__(self, paths: RetrievalPaths) -> None:
        self.paths = RetrievalPaths(
            data_root=paths.data_root.resolve(),
            catalog=paths.catalog.resolve(),
            v2_root=paths.v2_root.resolve(),
        )
        try:
            self.paths.catalog.relative_to(self.paths.data_root)
        except ValueError as exc:
            raise RetrievalDispatchStateError(
                "native catalog escapes the selected data root"
            ) from exc
        repository = CatalogRepository(
            self.paths.catalog,
            data_root=self.paths.data_root,
        )
        try:
            reader = NativeRetrievalReader(repository)
        except Exception:
            repository.close()
            raise
        self.repository = repository
        self.reader = reader
        self._close_lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._closed

    def close(self) -> None:
        """Close owned catalog connections after all request leases finish."""

        with self._close_lock:
            if self._closed:
                return
            self.repository.close()
            self._closed = True


@dataclass(frozen=True)
class ResolvedRetrievalDispatch:
    """One dispatch decision; native decisions expose only the live holder."""

    mode: str
    paths: RetrievalPaths
    native: NativeReaderHolder | None
    selection: RuntimeSelection | None

    def __post_init__(self) -> None:
        if self.mode == "native":
            holder_dispatch = self.native is not None and self.selection is None
            empty_dispatch = (
                self.native is None
                and self.selection is not None
                and self.selection.is_empty
            )
            if not (holder_dispatch or empty_dispatch):
                raise RetrievalDispatchStateError(
                    "native dispatch must use a holder or an exact empty selection"
                )
        elif self.native is not None or self.selection is None:
            raise RetrievalDispatchStateError(
                "non-native dispatch must retain its fresh runtime selection"
            )


_HOLDERS_LOCK = threading.RLock()
_HOLDERS: dict[Path, NativeReaderHolder] = {}


def prime_native_dispatch(selection: RuntimeSelection) -> NativeReaderHolder:
    """Initialize or return the one native holder for ``selection``'s root."""

    if not selection.is_native or selection.is_empty:
        raise RetrievalDispatchStateError(
            "only a validated non-empty native runtime can prime native dispatch"
        )
    with _HOLDERS_LOCK:
        return _prime_native_dispatch_locked(selection.paths)


def resolve_retrieval_dispatch(
    data_root: str | Path,
    *,
    validate_snapshot: bool = True,
    catalog_validation: RuntimeValidationMode = "full",
) -> ResolvedRetrievalDispatch:
    """Resolve one request without repeatedly inspecting an established V2.

    Cold native resolution is serialized so inspection, repository creation,
    and reader creation each happen exactly once. A non-native result is never
    retained and will therefore be inspected again on the next request.
    """

    expected_paths = retrieval_paths(data_root)
    root = expected_paths.data_root.resolve()
    with _HOLDERS_LOCK:
        holder = _HOLDERS.get(root)
        if holder is not None:
            _require_matching_catalog(holder, expected_paths.catalog)
            return ResolvedRetrievalDispatch(
                mode="native",
                paths=holder.paths,
                native=holder,
                selection=None,
            )

        selection = inspect_runtime(
            root,
            validate_snapshot=validate_snapshot,
            catalog_validation=catalog_validation,
        )
        if selection.is_native and not validate_snapshot:
            raise RetrievalDispatchStateError(
                "cold native dispatch requires active snapshot validation"
            )
        if selection.is_native:
            if selection.is_empty:
                return ResolvedRetrievalDispatch(
                    mode="native",
                    paths=selection.paths,
                    native=None,
                    selection=selection,
                )
            holder = _prime_native_dispatch_locked(selection.paths)
            return ResolvedRetrievalDispatch(
                mode="native",
                paths=holder.paths,
                native=holder,
                selection=None,
            )
        return ResolvedRetrievalDispatch(
            mode=selection.mode,
            paths=selection.paths,
            native=None,
            selection=selection,
        )


def reset_native_dispatchers(data_root: str | Path | None = None) -> None:
    """Close and forget native holders deterministically.

    Passing a root limits the reset to that installation. Without a root all
    process-scoped holders are closed. Repository close fails closed if a
    request is still leased, in which case that holder remains registered.
    """

    with _HOLDERS_LOCK:
        if data_root is None:
            roots = tuple(_HOLDERS)
        else:
            roots = (Path(data_root).resolve(),)
        for root in roots:
            holder = _HOLDERS.get(root)
            if holder is None:
                continue
            holder.close()
            if _HOLDERS.get(root) is holder:
                del _HOLDERS[root]


def _prime_native_dispatch_locked(paths: RetrievalPaths) -> NativeReaderHolder:
    root = paths.data_root.resolve()
    existing = _HOLDERS.get(root)
    if existing is not None:
        _require_matching_catalog(existing, paths.catalog)
        return existing
    holder = NativeReaderHolder(paths)
    _HOLDERS[root] = holder
    return holder


def _require_matching_catalog(
    holder: NativeReaderHolder,
    catalog_path: str | Path,
) -> None:
    if holder.paths.catalog != Path(catalog_path).resolve():
        raise RetrievalDispatchStateError(
            "one data root resolved to conflicting native catalog paths"
        )


def _close_at_exit() -> None:
    try:
        reset_native_dispatchers()
    except Exception:
        # Interpreter shutdown cannot safely recover an active request. Normal
        # application and test shutdown use the explicit reset surface.
        pass


atexit.register(_close_at_exit)


__all__ = [
    "NativeReaderHolder",
    "ResolvedRetrievalDispatch",
    "RetrievalDispatchStateError",
    "prime_native_dispatch",
    "reset_native_dispatchers",
    "resolve_retrieval_dispatch",
]
