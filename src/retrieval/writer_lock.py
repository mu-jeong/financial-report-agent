"""Nonce-bearing single-writer lock for whole native build/publication runs."""

from __future__ import annotations

import ctypes
import json
import math
import os
import socket
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.retrieval.identity import canonical_json


class WriterLockError(RuntimeError):
    """Raised when exclusive native writer ownership cannot be proved."""


@dataclass(frozen=True)
class ProcessState:
    alive: bool
    identity: str | None


ProcessProbe = Callable[[int], ProcessState]


@dataclass(frozen=True)
class WriterLease:
    """Immutable proof that one process still owns a specific lock nonce."""

    data_root: Path
    lock_path: Path
    hostname: str
    pid: int
    process_identity: str
    nonce: str
    created_at_unix: float
    guard_path: Path
    _guard_descriptor: int
    _probe: ProcessProbe
    _proof: object = field(repr=False, compare=False)

    def assert_owned(self, data_root: str | Path) -> None:
        """Fail closed unless this exact nonce is still owned for ``data_root``."""

        if not _lease_proof_is_active(self):
            raise WriterLockError("writer lease is no longer owned")
        try:
            requested_root = Path(data_root).resolve(strict=True)
        except OSError as exc:
            raise WriterLockError("writer lease data root is unavailable") from exc
        if requested_root != self.data_root:
            raise WriterLockError("writer lease belongs to a different data root")
        if os.getpid() != self.pid:
            raise WriterLockError("writer lease belongs to a different process")
        if not _guard_descriptor_is_current(
            self.guard_path,
            self._guard_descriptor,
        ):
            raise WriterLockError("writer lease is no longer owned")
        try:
            record = _validated_owner_record(_read_record(self.lock_path))
        except (OSError, ValueError) as exc:
            raise WriterLockError("writer lease is no longer owned") from exc
        if (
            record["hostname"] != self.hostname
            or record["pid"] != self.pid
            or record["process_identity"] != self.process_identity
            or record["nonce"] != self.nonce
            or record["created_at_unix"] != self.created_at_unix
        ):
            raise WriterLockError("writer lease is no longer owned")
        state = self._probe(self.pid)
        if not state.alive or state.identity != self.process_identity:
            raise WriterLockError("writer lease process identity is no longer current")


def assert_writer_lease_owned(
    writer_lease: object,
    data_root: str | Path,
) -> WriterLease:
    """Validate an exact module-issued lease before any native mutation."""

    if type(writer_lease) is not WriterLease:
        raise WriterLockError("writer lease is invalid")
    writer_lease.assert_owned(data_root)
    return writer_lease


class NativeWriterLock:
    """Own one data-root writer lock from candidate planning through publish."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        stale_after_seconds: float = 60.0,
        process_probe: ProcessProbe | None = None,
        clock: Callable[[], float] = time.time,
        hostname: str | None = None,
    ) -> None:
        if stale_after_seconds < 0:
            raise ValueError("stale_after_seconds cannot be negative")
        self.data_root = Path(data_root).resolve(strict=True)
        self.lock_path = self.data_root / "retrieval" / "v2" / "writer.lock"
        self.guard_path = self.data_root / "retrieval" / "v2" / "writer.guard"
        self.stale_after_seconds = float(stale_after_seconds)
        self._probe = process_probe or probe_process
        self._clock = clock
        self._hostname = hostname or socket.gethostname()
        if not isinstance(self._hostname, str) or not self._hostname.strip():
            raise ValueError("hostname must be a non-empty string")
        self._nonce: str | None = None
        self._record: dict[str, object] | None = None
        self._lease: WriterLease | None = None
        self._guard_descriptor: int | None = None

    def acquire(self) -> "NativeWriterLock":
        if self._nonce is not None or self._guard_descriptor is not None:
            raise WriterLockError("writer lock instance is already acquired")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._guard_descriptor = _acquire_process_guard(self.guard_path)
        try:
            for _attempt in range(4):
                nonce = uuid.uuid4().hex
                pid = os.getpid()
                state = self._probe(pid)
                if (
                    not state.alive
                    or not isinstance(state.identity, str)
                    or not state.identity.strip()
                ):
                    raise WriterLockError("current process identity cannot be verified")
                record = {
                    "schema_version": 1,
                    "hostname": self._hostname,
                    "pid": pid,
                    "process_identity": state.identity,
                    "nonce": nonce,
                    "created_at_unix": self._clock(),
                }
                encoded = (canonical_json(record) + "\n").encode("utf-8")
                try:
                    self._publish_owner_record(record, encoded)
                except FileExistsError:
                    self._reclaim_if_provably_stale()
                    continue
                lease = WriterLease(
                    data_root=self.data_root,
                    lock_path=self.lock_path,
                    hostname=self._hostname,
                    pid=pid,
                    process_identity=state.identity,
                    nonce=nonce,
                    created_at_unix=float(record["created_at_unix"]),
                    guard_path=self.guard_path,
                    _guard_descriptor=self._guard_descriptor,
                    _probe=self._probe,
                    _proof=object(),
                )
                _register_active_lease(lease)
                self._nonce = nonce
                self._record = record
                self._lease = lease
                return self
            raise WriterLockError("native writer lock is already owned")
        except BaseException:
            self._release_guard()
            raise

    def _publish_owner_record(
        self,
        record: dict[str, object],
        encoded: bytes,
    ) -> None:
        """Durably publish a complete record without exposing partial bytes."""

        nonce = str(record["nonce"])
        temporary_path = self.lock_path.parent / f"writer-record-{nonce}.tmp"
        temporary_created = False
        try:
            descriptor = os.open(
                temporary_path,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            temporary_created = True
            try:
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

            # A same-directory hard link gives the authoritative name
            # no-overwrite semantics on both POSIX and Windows.  The link is
            # created only after the temporary inode contains a complete,
            # file-synced record.
            os.link(temporary_path, self.lock_path)
            temporary_path.unlink()
            _fsync_directory(self.lock_path.parent)
        except BaseException as exc:
            try:
                self._cleanup_failed_owner_record(
                    temporary_path,
                    record,
                    remove_temporary=temporary_created,
                    allow_unrelated_lock=isinstance(exc, FileExistsError),
                )
            except BaseException as cleanup_exc:
                raise WriterLockError(
                    "failed writer owner record could not be safely removed: "
                    f"{cleanup_exc}"
                ) from exc
            raise

    def _cleanup_failed_owner_record(
        self,
        temporary_path: Path,
        expected_record: dict[str, object],
        *,
        remove_temporary: bool,
        allow_unrelated_lock: bool,
    ) -> None:
        """Remove only artifacts proved to belong to this publication attempt."""

        if remove_temporary:
            self._remove_or_quarantine_temporary(temporary_path, expected_record)

        try:
            exact_owner = _path_has_exact_record(self.lock_path, expected_record)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            if allow_unrelated_lock:
                return
            raise WriterLockError(
                "failed writer owner record cannot be identified"
            ) from exc

        if not exact_owner:
            if allow_unrelated_lock:
                return
            raise WriterLockError(
                "failed writer owner record identity changed; refusing cleanup"
            )

        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            quarantine_path = self.lock_path.parent / (
                f"writer-failed-{expected_record['nonce']}-{uuid.uuid4().hex[:8]}.json"
            )
            os.replace(self.lock_path, quarantine_path)
        _fsync_directory(self.lock_path.parent)

    def _remove_or_quarantine_temporary(
        self,
        temporary_path: Path,
        expected_record: dict[str, object],
    ) -> None:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            quarantine_path = self.lock_path.parent / (
                f"writer-failed-{expected_record['nonce']}-{uuid.uuid4().hex[:8]}.tmp"
            )
            os.replace(temporary_path, quarantine_path)

    @property
    def lease(self) -> WriterLease:
        if self._lease is None:
            raise WriterLockError("writer lock is not acquired")
        return assert_writer_lease_owned(self._lease, self.data_root)

    def release(self) -> None:
        if self._nonce is None or self._record is None:
            _unregister_active_lease(self._lease)
            self._lease = None
            self._release_guard()
            return
        error: WriterLockError | None = None
        lease = self._lease
        try:
            assert lease is not None
            try:
                assert_writer_lease_owned(lease, self.data_root)
            except WriterLockError as exc:
                raise WriterLockError(
                    "writer lock ownership changed; refusing to unlink it"
                ) from exc
            self.lock_path.unlink()
            _fsync_directory(self.lock_path.parent)
        except WriterLockError as exc:
            error = exc
        except OSError as exc:
            error = WriterLockError(
                "writer lock ownership changed; refusing to unlink it"
            )
            error.__cause__ = exc
        finally:
            self._nonce = None
            self._record = None
            self._lease = None
            _unregister_active_lease(lease)
            self._release_guard()
        if error is not None:
            raise error

    def __enter__(self) -> WriterLease:
        self.acquire()
        return self.lease

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def _reclaim_if_provably_stale(self) -> None:
        try:
            stat_result = self.lock_path.stat()
            record = _read_record(self.lock_path)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise WriterLockError(
                "writer lock cannot be proved stale because its record is unreadable"
            ) from exc
        try:
            owner = _validated_owner_record(record)
        except ValueError as exc:
            raise WriterLockError("writer lock has an invalid owner record") from exc
        age = self._clock() - max(owner["created_at_unix"], stat_result.st_mtime)
        if owner["hostname"] != self._hostname:
            raise WriterLockError("writer lock belongs to another host")
        pid = owner["pid"]
        identity = owner["process_identity"]
        state = self._probe(pid)
        if state.alive and state.identity == identity:
            raise WriterLockError("native writer lock is already owned by a live process")
        if state.alive and state.identity is None:
            raise WriterLockError("writer owner is alive but its identity is unavailable")
        if age < self.stale_after_seconds:
            raise WriterLockError("writer owner changed but lock is not stale yet")
        # Either the owner is gone or the PID has been reused with a different
        # creation identity.  Both facts are stronger than PID-only staleness.
        self._quarantine_stale_lock()

    def _quarantine_stale_lock(self) -> None:
        target = self.lock_path.parent / f"writer-stale-{uuid.uuid4().hex[:12]}.json"
        try:
            os.replace(self.lock_path, target)
        except FileNotFoundError:
            return
        _fsync_directory(self.lock_path.parent)

    def _release_guard(self) -> None:
        descriptor = self._guard_descriptor
        self._guard_descriptor = None
        if descriptor is not None:
            _release_process_guard(descriptor)


_ACTIVE_LEASES: dict[object, WriterLease] = {}
_ACTIVE_LEASES_LOCK = threading.Lock()


def _register_active_lease(lease: WriterLease) -> None:
    with _ACTIVE_LEASES_LOCK:
        if lease._proof in _ACTIVE_LEASES:
            raise WriterLockError("writer lease proof is already registered")
        _ACTIVE_LEASES[lease._proof] = lease


def _unregister_active_lease(lease: WriterLease | None) -> None:
    if lease is None:
        return
    with _ACTIVE_LEASES_LOCK:
        if _ACTIVE_LEASES.get(lease._proof) is lease:
            del _ACTIVE_LEASES[lease._proof]


def _lease_proof_is_active(lease: WriterLease) -> bool:
    with _ACTIVE_LEASES_LOCK:
        return _ACTIVE_LEASES.get(lease._proof) is lease


def probe_process(pid: int) -> ProcessState:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return ProcessState(False, None)
    if os.name == "nt":
        return _probe_windows_process(pid)
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        value = proc_stat.read_text(encoding="utf-8")
        fields = value[value.rfind(")") + 2 :].split()
        return ProcessState(True, f"proc-start:{fields[19]}")
    except FileNotFoundError:
        return ProcessState(False, None)
    except (OSError, IndexError):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ProcessState(False, None)
        except PermissionError:
            return ProcessState(True, None)
        return ProcessState(True, None)


def _probe_windows_process(pid: int) -> ProcessState:
    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process.
            return ProcessState(False, None)
        return ProcessState(True, None)
    try:
        creation = FILETIME()
        exit_time = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return ProcessState(True, None)
        value = (int(creation.high) << 32) | int(creation.low)
        return ProcessState(True, f"win-filetime:{value}")
    finally:
        kernel32.CloseHandle(handle)


def _read_record(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported writer lock record")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0 or written > len(payload) - offset:
            raise OSError("writer owner record write made no progress")
        offset += written


def _path_has_exact_record(
    path: Path,
    expected_record: dict[str, object],
) -> bool:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("writer owner record is not a regular file")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not _same_file_identity(before, opened):
            raise ValueError("writer owner record identity changed")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            size += len(chunk)
            if size > 65_536:
                raise ValueError("writer owner record is unexpectedly large")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    after = path.lstat()
    if not _same_file_identity(opened, after):
        raise ValueError("writer owner record identity changed")
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported writer lock record")
    return _validated_owner_record(payload) == _validated_owner_record(expected_record)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(first.st_mode)
        and stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
    )


def _validated_owner_record(record: dict[str, object]) -> dict[str, object]:
    if set(record) != {
        "schema_version",
        "hostname",
        "pid",
        "process_identity",
        "nonce",
        "created_at_unix",
    }:
        raise ValueError("unexpected owner record fields")
    hostname = record.get("hostname")
    pid = record.get("pid")
    identity = record.get("process_identity")
    nonce = record.get("nonce")
    created_at = record.get("created_at_unix")
    if not isinstance(hostname, str) or not hostname.strip():
        raise ValueError("invalid hostname")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("invalid pid")
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("invalid process identity")
    if not isinstance(nonce, str) or not nonce.strip():
        raise ValueError("invalid nonce")
    if (
        not isinstance(created_at, (int, float))
        or isinstance(created_at, bool)
        or not math.isfinite(float(created_at))
    ):
        raise ValueError("invalid creation time")
    return {
        "hostname": hostname,
        "pid": pid,
        "process_identity": identity,
        "nonce": nonce,
        "created_at_unix": float(created_at),
    }


def _acquire_process_guard(path: Path) -> int:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WriterLockError("writer process guard is unsafe")
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except OSError as exc:
        raise WriterLockError("writer process guard cannot be opened") from exc
    try:
        if os.fstat(descriptor).st_size == 0:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        _lock_process_guard(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, 2) != b"\0":
            raise WriterLockError("writer process guard is malformed")
        if not _guard_descriptor_is_current(path, descriptor):
            raise WriterLockError("writer process guard identity changed")
        return descriptor
    except (BlockingIOError, PermissionError) as exc:
        os.close(descriptor)
        raise WriterLockError(
            "native writer lock is already owned by a live process"
        ) from exc
    except BaseException:
        try:
            _unlock_process_guard(descriptor)
        except OSError:
            pass
        os.close(descriptor)
        raise


def _lock_process_guard(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_process_guard(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _release_process_guard(descriptor: int) -> None:
    try:
        _unlock_process_guard(descriptor)
    except OSError:
        # Closing the descriptor releases the OS lock even if an explicit
        # unlock reports a teardown-time error.
        pass
    finally:
        os.close(descriptor)


def _guard_descriptor_is_current(path: Path, descriptor: int) -> bool:
    try:
        opened = os.fstat(descriptor)
        current = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and opened.st_dev == current.st_dev
        and opened.st_ino == current.st_ino
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "NativeWriterLock",
    "ProcessState",
    "WriterLease",
    "WriterLockError",
    "assert_writer_lease_owned",
    "probe_process",
]
