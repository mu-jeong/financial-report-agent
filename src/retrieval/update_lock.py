"""Cross-process fence shared by supported retrieval update entrypoints.

The lock lives directly under the stable data root, outside ``retrieval/``, so
it continues to fence V1 writers while a staged V2 directory is activated.
The small guard file is persistent; operating-system ownership is released
automatically if the owning process exits.
"""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO


class RetrievalUpdateLockError(RuntimeError):
    """Raised when another supported updater or migration owns the fence."""


class RetrievalUpdateLock(AbstractContextManager["RetrievalUpdateLock"]):
    """Hold one non-blocking, cross-process update fence for a data root."""

    FILE_NAME = ".retrieval-update.guard"

    def __init__(self, data_root: str | Path) -> None:
        root = Path(data_root).expanduser().absolute()
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise RetrievalUpdateLockError("retrieval data root is unavailable") from exc
        if not root.is_dir() or root.is_symlink():
            raise RetrievalUpdateLockError("retrieval data root must be a real directory")
        self.data_root = root
        self.path = root / self.FILE_NAME
        self._stream: BinaryIO | None = None

    def acquire(self) -> "RetrievalUpdateLock":
        if self._stream is not None:
            raise RetrievalUpdateLockError("retrieval update lock is already acquired")
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise RetrievalUpdateLockError("retrieval update guard is not a real file")
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RetrievalUpdateLockError(
                "another retrieval update or V2 migration is already running"
            ) from exc
        self._stream = stream
        return self

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> "RetrievalUpdateLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
        return None


__all__ = ["RetrievalUpdateLock", "RetrievalUpdateLockError"]
