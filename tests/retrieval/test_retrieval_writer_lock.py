from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import replace
from pathlib import Path

import pytest

import src.retrieval.writer_lock as writer_lock_module
from src.retrieval.writer_lock import (
    NativeWriterLock,
    ProcessState,
    WriterLockBusyError,
    WriterLockError,
    assert_writer_lease_owned,
)


def _attempt_writer_lock_in_child(data_root: str, output) -> None:
    try:
        lock = NativeWriterLock(data_root).acquire()
    except WriterLockError:
        output.put("blocked")
        return
    try:
        output.put("acquired")
    finally:
        lock.release()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "data root 한글"
    root.mkdir()
    return root


def _initialize_process_guard(root: Path) -> None:
    with NativeWriterLock(root):
        pass


def test_live_nonce_owner_blocks_second_writer_and_release_allows_next(tmp_path: Path):
    root = _root(tmp_path)
    identity = "process-start-1"
    probe = lambda pid: ProcessState(True, identity)
    first = NativeWriterLock(root, process_probe=probe)

    with first:
        with pytest.raises(WriterLockBusyError, match="live process"):
            NativeWriterLock(root, process_probe=probe).acquire()

    with NativeWriterLock(root, process_probe=probe):
        assert (root / "retrieval" / "v2" / "writer.lock").is_file()
    assert not (root / "retrieval" / "v2" / "writer.lock").exists()


def test_process_guard_blocks_an_independent_writer_process(tmp_path: Path):
    root = _root(tmp_path)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()

    with NativeWriterLock(root):
        child = context.Process(
            target=_attempt_writer_lock_in_child,
            args=(str(root), output),
        )
        child.start()
        child.join(timeout=15)

    assert not child.is_alive()
    assert child.exitcode == 0
    assert output.get(timeout=2) == "blocked"


def test_one_lock_instance_cannot_reacquire_and_release_its_guard(tmp_path: Path):
    root = _root(tmp_path)
    probe = lambda pid: ProcessState(True, "current-process")
    lock = NativeWriterLock(root, process_probe=probe).acquire()

    with pytest.raises(WriterLockError, match="already acquired"):
        lock.acquire()

    lock.lease.assert_owned(root)
    lock.release()


def test_provably_dead_stale_owner_is_quarantined(tmp_path: Path):
    root = _root(tmp_path)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hostname": "host",
                "pid": 999,
                "process_identity": "old",
                "nonce": "old-nonce",
                "created_at_unix": 1.0,
            }
        ),
        encoding="utf-8",
    )
    os.utime(lock_path, (1.0, 1.0))
    probe = lambda pid: (
        ProcessState(True, "current") if pid == os.getpid() else ProcessState(False, None)
    )

    with NativeWriterLock(
        root,
        process_probe=probe,
        hostname="host",
        stale_after_seconds=10,
        clock=lambda: 100.0,
    ):
        assert lock_path.is_file()

    assert len(list(lock_path.parent.glob("writer-stale-*.json"))) == 1


def test_stale_reclaim_guard_prevents_a_delayed_second_reclaimer(
    tmp_path: Path,
):
    root = _root(tmp_path)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hostname": "host",
                "pid": 999,
                "process_identity": "dead-process",
                "nonce": "dead-nonce",
                "created_at_unix": 1.0,
            }
        ),
        encoding="utf-8",
    )
    os.utime(lock_path, (1.0, 1.0))
    first_probe = lambda pid: (
        ProcessState(True, "current-process")
        if pid == os.getpid()
        else ProcessState(False, None)
    )

    with NativeWriterLock(
        root,
        process_probe=first_probe,
        hostname="host",
        stale_after_seconds=0,
        clock=lambda: 100.0,
    ):
        live_record = lock_path.read_bytes()

        def second_probe(_pid: int) -> ProcessState:
            pytest.fail("second reclaimer crossed the process guard")

        with pytest.raises(WriterLockError, match="live process"):
            NativeWriterLock(
                root,
                process_probe=second_probe,
                hostname="host",
                stale_after_seconds=0,
                clock=lambda: 100.0,
            ).acquire()

        assert lock_path.read_bytes() == live_record
        assert len(list(lock_path.parent.glob("writer-stale-*.json"))) == 1


def test_pid_reuse_requires_different_creation_identity_and_stale_age(tmp_path: Path):
    root = _root(tmp_path)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hostname": "host",
                "pid": 777,
                "process_identity": "old-process",
                "nonce": "old-nonce",
                "created_at_unix": 95.0,
            }
        ),
        encoding="utf-8",
    )
    os.utime(lock_path, (95.0, 95.0))
    probe = lambda pid: (
        ProcessState(True, "current-process")
        if pid == os.getpid()
        else ProcessState(True, "reused-process")
    )

    with pytest.raises(WriterLockError, match="not stale yet"):
        NativeWriterLock(
            root,
            process_probe=probe,
            hostname="host",
            stale_after_seconds=10,
            clock=lambda: 100.0,
        ).acquire()


def test_arbitrarily_old_malformed_lock_is_never_reclaimed(tmp_path: Path):
    root = _root(tmp_path)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("{not-json", encoding="utf-8")
    os.utime(lock_path, (1.0, 1.0))
    probe = lambda pid: ProcessState(True, "current-process")

    with pytest.raises(WriterLockError, match="cannot be proved stale"):
        NativeWriterLock(
            root,
            process_probe=probe,
            stale_after_seconds=0,
            clock=lambda: 1_000_000.0,
        ).acquire()

    assert lock_path.read_text(encoding="utf-8") == "{not-json"
    assert not list(lock_path.parent.glob("writer-stale-*.json"))


def test_failed_malformed_reclaim_releases_the_process_guard(tmp_path: Path):
    root = _root(tmp_path)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("{not-json", encoding="utf-8")
    probe = lambda pid: ProcessState(True, "current-process")

    with pytest.raises(WriterLockError, match="cannot be proved stale"):
        NativeWriterLock(root, process_probe=probe).acquire()

    lock_path.unlink()
    with NativeWriterLock(root, process_probe=probe):
        assert lock_path.is_file()


def test_record_write_failure_never_leaves_an_authoritative_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _root(tmp_path)
    _initialize_process_guard(root)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    monkeypatch.setattr(
        writer_lock_module.os,
        "write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected record write failure")
        ),
    )

    with pytest.raises(OSError, match="record write failure"):
        NativeWriterLock(root).acquire()

    assert not lock_path.exists()


def test_partial_record_write_failure_never_publishes_partial_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _root(tmp_path)
    _initialize_process_guard(root)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    original_write = writer_lock_module.os.write
    calls = 0

    def partial_then_fail(descriptor: int, payload: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, payload[: max(1, len(payload) // 2)])
        raise OSError("injected partial record write failure")

    monkeypatch.setattr(writer_lock_module.os, "write", partial_then_fail)

    with pytest.raises(OSError, match="partial record write failure"):
        NativeWriterLock(root).acquire()

    assert not lock_path.exists()
    assert not list(lock_path.parent.glob("writer-record-*.tmp"))


def test_record_file_fsync_failure_never_leaves_an_authoritative_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _root(tmp_path)
    _initialize_process_guard(root)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    monkeypatch.setattr(
        writer_lock_module.os,
        "fsync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected record fsync failure")
        ),
    )

    with pytest.raises(OSError, match="record fsync failure"):
        NativeWriterLock(root).acquire()

    assert not lock_path.exists()


def test_record_directory_fsync_failure_removes_the_exact_published_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _root(tmp_path)
    _initialize_process_guard(root)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    original_fsync_directory = writer_lock_module._fsync_directory
    calls = 0

    def fail_once(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(writer_lock_module, "_fsync_directory", fail_once)

    with pytest.raises(OSError, match="directory fsync failure"):
        NativeWriterLock(root).acquire()

    assert not lock_path.exists()
    assert not list(lock_path.parent.glob("writer-record-*.tmp"))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("hostname", ""),
        ("pid", 0),
        ("process_identity", ""),
        ("nonce", ""),
        ("created_at_unix", float("nan")),
        ("unexpected", "ambiguous"),
    ),
)
def test_invalid_owner_record_is_never_reclaimed(
    tmp_path: Path,
    field: str,
    value: object,
):
    root = _root(tmp_path)
    lock_path = root / "retrieval" / "v2" / "writer.lock"
    lock_path.parent.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "hostname": "host",
        "pid": 777,
        "process_identity": "old-process",
        "nonce": "old-nonce",
        "created_at_unix": 1.0,
    }
    record[field] = value
    lock_path.write_text(json.dumps(record), encoding="utf-8")
    os.utime(lock_path, (1.0, 1.0))
    probe = lambda pid: ProcessState(True, "current-process")

    with pytest.raises(WriterLockError, match="invalid owner record"):
        NativeWriterLock(
            root,
            process_probe=probe,
            hostname="host",
            stale_after_seconds=0,
            clock=lambda: 1_000_000.0,
        ).acquire()

    assert lock_path.exists()
    assert not list(lock_path.parent.glob("writer-stale-*.json"))


def test_writer_lease_proves_current_nonce_and_root(tmp_path: Path):
    root = _root(tmp_path)
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    probe = lambda pid: ProcessState(True, "current-process")

    with NativeWriterLock(root, process_probe=probe) as lease:
        lease.assert_owned(root)
        with pytest.raises(WriterLockError, match="different data root"):
            lease.assert_owned(other_root)

    with pytest.raises(WriterLockError, match="no longer owned"):
        lease.assert_owned(root)


def test_copied_writer_lease_cannot_reuse_an_active_proof(tmp_path: Path):
    root = _root(tmp_path)
    probe = lambda pid: ProcessState(True, "current-process")

    with NativeWriterLock(root, process_probe=probe) as lease:
        copied = replace(lease)

        with pytest.raises(WriterLockError, match="no longer owned"):
            assert_writer_lease_owned(copied, root)
        assert_writer_lease_owned(lease, root)

        with pytest.raises(WriterLockError, match="invalid"):
            assert_writer_lease_owned(object(), root)


def test_release_never_unlinks_another_nonce(tmp_path: Path):
    root = _root(tmp_path)
    probe = lambda pid: ProcessState(True, "current-process")
    lock = NativeWriterLock(root, process_probe=probe).acquire()
    path = root / "retrieval" / "v2" / "writer.lock"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["nonce"] = "replacement"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(WriterLockError, match="ownership changed"):
        lock.release()

    assert path.exists()
