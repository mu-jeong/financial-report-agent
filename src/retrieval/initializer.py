"""Atomic greenfield initialization for the Native V2 catalog."""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

from src.retrieval.schema import checkpoint_isolated_catalog, install_schema
from src.retrieval.writer_lock import (
    WriterLease,
    WriterLockError,
    assert_writer_lease_owned,
    ensure_native_runtime_directory,
)


class NativeInitializationError(RuntimeError):
    """Raised when an empty Native V2 catalog cannot be published safely."""


_TEMP_PREFIX = "catalog.empty."
_TEMP_SUFFIX = ".tmp"


def initialize_empty_native(
    data_root: str | Path,
    *,
    writer_lease: WriterLease,
    crash_after: str | None = None,
) -> Path:
    """Publish the exact ``native/empty`` singleton for a greenfield root.

    The caller owns ``RetrievalUpdateLock`` and passes the nested native writer
    lease.
    """

    root = Path(data_root).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise NativeInitializationError("data root must be a real local directory")
    assert_writer_lease_owned(writer_lease, root)

    try:
        v2_root = ensure_native_runtime_directory(root)
    except WriterLockError as exc:
        raise NativeInitializationError(str(exc)) from exc
    catalog = v2_root / "catalog.sqlite3"
    if catalog.exists():
        if catalog.is_symlink() or not catalog.is_file():
            raise NativeInitializationError("native catalog must be a real local file")
        return catalog

    _remove_abandoned_empty_catalogs(v2_root)

    temp = v2_root / f"{_TEMP_PREFIX}{uuid.uuid4().hex}{_TEMP_SUFFIX}"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temp)
        install_schema(connection)
        connection.commit()
        checkpoint_isolated_catalog(connection)
        connection.close()
        connection = None
        _fsync_file(temp)
        _crash("schema_committed", crash_after)

        if catalog.exists():
            raise NativeInitializationError("native catalog appeared during initialization")
        os.replace(temp, catalog)
        _fsync_file(catalog)
        _fsync_directory(v2_root)
        _crash("catalog_replaced", crash_after)
        return catalog
    except NativeInitializationError:
        raise
    except Exception as exc:
        raise NativeInitializationError(f"native empty catalog initialization failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        if temp.exists():
            temp.unlink()
        for sidecar in (Path(f"{temp}-wal"), Path(f"{temp}-shm")):
            if sidecar.exists():
                sidecar.unlink()


def _remove_abandoned_empty_catalogs(v2_root: Path) -> None:
    """Remove only initializer-owned temp files after validating their names."""

    candidates = tuple(v2_root.iterdir())
    bases = tuple(candidate for candidate in candidates if _is_owned_base(candidate))
    for candidate in bases:
        _unlink_owned_file(candidate)
        for sidecar in (Path(f"{candidate}-wal"), Path(f"{candidate}-shm")):
            if sidecar.exists() or sidecar.is_symlink():
                _unlink_owned_file(sidecar)
    for candidate in candidates:
        if candidate.exists() and _is_owned_sidecar(candidate):
            _unlink_owned_file(candidate)


def _owned_token(name: str, suffix: str) -> str | None:
    if not name.startswith(_TEMP_PREFIX) or not name.endswith(suffix):
        return None
    token = name[len(_TEMP_PREFIX) : -len(suffix)]
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        return None
    return token


def _is_owned_base(path: Path) -> bool:
    return _owned_token(path.name, _TEMP_SUFFIX) is not None


def _is_owned_sidecar(path: Path) -> bool:
    return any(
        _owned_token(path.name, suffix) is not None
        for suffix in (f"{_TEMP_SUFFIX}-wal", f"{_TEMP_SUFFIX}-shm")
    )


def _unlink_owned_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise NativeInitializationError("abandoned initializer path is not a regular file")
    path.unlink()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _crash(boundary: str, crash_after: str | None) -> None:
    if crash_after == boundary:
        raise NativeInitializationError(f"injected initialization crash after {boundary}")


__all__ = [
    "NativeInitializationError",
    "initialize_empty_native",
]
