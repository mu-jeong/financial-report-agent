"""Concurrent first-successor bootstrap and writer-gate verification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from src.retrieval.bootstrap import (
    RetrievalBootstrapError,
    RuntimeSelection,
    inspect_runtime,
    reconcile_and_inspect_runtime,
)
from src.retrieval.build_service import CandidateResult, publish_candidate
from src.retrieval.identity import canonical_json
from src.retrieval.publication import PublicationOutcome
from src.retrieval.runtime_guard import (
    RetrievalWriteBlocked,
    guard_before_retrieval_write,
)
from src.retrieval.writer_lock import NativeWriterLock, WriterLease


_INSTALL_LABELS = ("source-default", "packaged-default")
_LAUNCHER_LAYOUT = (
    "apps/cli/app.py",
    "apps/gui/app.py",
    "quickstart.py",
    "RUN_APP.bat",
    "RUN_QUICKSTART.bat",
    "MIGRATE_V2.bat",
    "REBUILD_V2.bat",
    "scripts/migrations/v2/migrate_v2_user.py",
    "scripts/migrations/v2/rebuild_v2_successor.py",
    "src/retrieval/launcher_guard.py",
    "src/retrieval/update_lock.py",
)


class LauncherRaceError(RuntimeError):
    """Raised when the first-successor race observes an unsafe state."""


@dataclass(frozen=True)
class LauncherRaceResult:
    candidate: CandidateResult
    publication: PublicationOutcome
    evidence: dict[str, Any]


def publish_candidate_with_launcher_race(
    result: CandidateResult | None,
    data_root: str | Path,
    *,
    candidate_factory: Callable[[WriterLease], CandidateResult] | None = None,
    install_roots: Mapping[str, str | Path] | None = None,
    installed_baseline: Mapping[str, Any] | None = None,
    worker_count: int = 6,
    max_probes_per_worker: int = 100,
    process_timeout_seconds: int = 60,
) -> LauncherRaceResult:
    """Publish once while canonical launcher and updater selectors race it."""

    if worker_count < 2:
        raise ValueError("launcher race requires at least two workers")
    if max_probes_per_worker <= 0:
        raise ValueError("max probes per worker must be positive")
    if process_timeout_seconds <= 0:
        raise ValueError("process timeout must be positive")
    if (result is None) == (candidate_factory is None):
        raise ValueError("provide exactly one candidate result or candidate factory")
    root = Path(data_root).resolve(strict=True)
    legacy_anchor = root / "reports.db"
    before = inspect_runtime(legacy_anchor, data_root=root, validate_snapshot=True)
    if (
        before.mode != "native"
        or before.write_epoch != 0
        or not before.v1_fallback_open
        or before.write_enabled
    ):
        raise LauncherRaceError("launcher race must start from the epoch-zero seed")
    if installed_baseline is not None and install_roots is None:
        raise LauncherRaceError("installed baseline requires installed roots")
    installed = None if install_roots is None else _validated_install_roots(install_roots)
    installed_waves: dict[str, Any] | None = None
    if installed is not None:
        if installed_baseline is None:
            pre_wave = _run_installed_wave(
                installed,
                legacy_anchor,
                timeout_seconds=process_timeout_seconds,
            )
            layout_sha256 = _install_layout_hashes(installed)
            if len(set(layout_sha256.values())) != 1:
                raise LauncherRaceError(
                    "source and packaged launcher layouts do not match"
                )
        else:
            pre_wave, layout_sha256 = _validated_installed_baseline(
                installed_baseline,
                installed,
                _runtime_identity(before),
            )
        _require_wave_identity(pre_wave, _runtime_identity(before), "before")
        installed_waves = {
            "launcher_layout_sha256": layout_sha256,
            "before": pre_wave,
        }

    barrier = threading.Barrier(worker_count + 1)
    publish_started = threading.Event()
    finished = threading.Event()
    condition = threading.Condition()
    observations: list[tuple[str, str, tuple[Any, ...] | None]] = []
    errors: list[str] = []

    def record(surface: str, disposition: str, selection: RuntimeSelection | None) -> None:
        identity = None if selection is None else _runtime_identity(selection)
        with condition:
            observations.append((surface, disposition, identity))
            condition.notify_all()

    def probe_once(surface: str, *, startup: bool) -> None:
        try:
            if surface == "launcher":
                selection = (
                    reconcile_and_inspect_runtime(
                        legacy_anchor,
                        data_root=root,
                    )
                    if startup
                    else inspect_runtime(
                        legacy_anchor,
                        data_root=root,
                        validate_snapshot=True,
                    )
                )
            else:
                selection = guard_before_retrieval_write(
                    legacy_anchor,
                    data_root=root,
                )
            record(surface, "selected", selection)
        except (RetrievalBootstrapError, RetrievalWriteBlocked):
            record(surface, "fail_closed", None)
        except BaseException as exc:
            with condition:
                errors.append(type(exc).__name__)
                condition.notify_all()

    def probe(worker_number: int) -> None:
        surface = "launcher" if worker_number % 2 == 0 else "updater"
        barrier.wait()
        probe_once(surface, startup=False)
        publish_started.wait()
        for _ in range(max_probes_per_worker):
            if finished.is_set():
                break
            probe_once(surface, startup=True)

    workers = [
        threading.Thread(target=probe, args=(number,), daemon=True)
        for number in range(worker_count)
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    deadline = time.monotonic() + 30
    with condition:
        while len(observations) < worker_count and not errors:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            condition.wait(timeout=remaining)
    if errors or len(observations) < worker_count:
        publish_started.set()
        finished.set()
        for worker in workers:
            worker.join(timeout=30)
        raise LauncherRaceError("launcher race could not establish initial probes")
    initial_surfaces = {surface for surface, _disposition, _identity in observations}
    if initial_surfaces != {"launcher", "updater"}:
        publish_started.set()
        finished.set()
        for worker in workers:
            worker.join(timeout=30)
        raise LauncherRaceError("launcher race did not cover both guarded surfaces")

    race_processes: list[dict[str, Any]] = []
    race_wave: list[dict[str, Any]] | None = None
    try:
        with NativeWriterLock(root) as writer_lease:
            candidate = (
                result
                if candidate_factory is None
                else candidate_factory(writer_lease)
            )
            if not isinstance(candidate, CandidateResult):
                raise TypeError("candidate factory must return CandidateResult")
            if installed is not None:
                assert installed_waves is not None
                if _install_layout_hashes(installed) != installed_waves[
                    "launcher_layout_sha256"
                ]:
                    raise LauncherRaceError(
                        "installed launcher layout changed before publication"
                    )
            publish_started.set()
            if installed is not None:
                race_processes = _start_installed_wave(installed, legacy_anchor)
            publication = publish_candidate(
                candidate,
                root,
                writer_lease=writer_lease,
            )
            if installed is not None:
                race_wave = _finish_installed_wave(
                    race_processes,
                    timeout_seconds=process_timeout_seconds,
                )
    except BaseException:
        publish_started.set()
        if race_processes and race_wave is None:
            _finish_installed_wave(
                race_processes,
                timeout_seconds=process_timeout_seconds,
            )
        finished.set()
        for worker in workers:
            worker.join(timeout=30)
        raise

    after = inspect_runtime(legacy_anchor, data_root=root, validate_snapshot=True)
    finished.set()
    for worker in workers:
        worker.join(timeout=30)
    if any(worker.is_alive() for worker in workers):
        raise LauncherRaceError("launcher race worker did not terminate")
    if errors:
        raise LauncherRaceError("launcher race worker failed unexpectedly")
    # Take the exact post-cutover samples only after transition workers stop;
    # otherwise their startup reconciliation locks can starve this final probe.
    probe_once("launcher", startup=True)
    probe_once("updater", startup=True)
    if errors:
        raise LauncherRaceError("launcher race post-cutover probe failed unexpectedly")

    if (
        after.mode != "native"
        or after.active_snapshot_id != publication.active_snapshot_id
        or after.write_epoch <= 0
        or after.v1_fallback_open
        or after.degraded
        or not after.write_enabled
    ):
        raise LauncherRaceError("launcher race did not finish on the writable successor")

    allowed = {_runtime_identity(before), _runtime_identity(after)}
    unsafe = [
        identity
        for _surface, disposition, identity in observations
        if disposition == "selected" and identity not in allowed
    ]
    if unsafe:
        raise LauncherRaceError("launcher race observed a non-authoritative runtime")
    dispositions = {
        (surface, disposition, identity)
        for surface, disposition, identity in observations
    }
    if (
        ("launcher", "selected", _runtime_identity(before)) not in dispositions
        or ("updater", "fail_closed", None) not in dispositions
        or ("launcher", "selected", _runtime_identity(after)) not in dispositions
        or ("updater", "selected", _runtime_identity(after)) not in dispositions
    ):
        raise LauncherRaceError("launcher race did not prove both sides of the cutover")
    if installed is not None:
        assert race_wave is not None
        _require_wave_safe_during_transition(
            race_wave,
            {_runtime_identity(before), _runtime_identity(after)},
        )
        post_concurrent_wave = _finish_installed_wave(
            _start_installed_wave(installed, legacy_anchor),
            timeout_seconds=process_timeout_seconds,
        )
        _require_wave_safe_at_boundary(
            post_concurrent_wave,
            _runtime_identity(after),
        )
        post_wave = _run_installed_wave(
            installed,
            legacy_anchor,
            timeout_seconds=process_timeout_seconds,
        )
        _require_wave_identity(post_wave, _runtime_identity(after), "after")
        assert installed_waves is not None
        if _install_layout_hashes(installed) != installed_waves[
            "launcher_layout_sha256"
        ]:
            raise LauncherRaceError("installed launcher layout changed during the race")
        installed_waves.update(
            race=race_wave,
            after_concurrent=post_concurrent_wave,
            after=post_wave,
        )
    counts = Counter(
        f"{surface}:{disposition}"
        for surface, disposition, _identity in observations
    )
    identity_hashes = sorted(
        {
            hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
            for _surface, _disposition, identity in observations
            if identity is not None
        }
    )
    evidence = {
        "schema_version": 1,
        "kind": "v2_first_successor_launcher_race",
        "worker_count": worker_count,
        "probe_count": len(observations),
        "observation_counts": dict(sorted(counts.items())),
        "observed_runtime_identity_sha256": identity_hashes,
        "before": _redacted_identity(before),
        "after": _redacted_identity(after),
        "publication_id": publication.publication_id,
        "installed_process_waves": installed_waves,
        "release_eligible": installed_waves is not None,
        "passed": True,
    }
    return LauncherRaceResult(
        candidate=candidate,
        publication=publication,
        evidence=evidence,
    )


def capture_epoch_zero_installed_baseline(
    data_root: str | Path,
    install_roots: Mapping[str, str | Path],
    *,
    process_timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Capture exact installed launcher behavior before taking the build lock."""

    if process_timeout_seconds <= 0:
        raise ValueError("process timeout must be positive")
    root = Path(data_root).resolve(strict=True)
    before = inspect_runtime(
        root / "reports.db",
        data_root=root,
        validate_snapshot=True,
    )
    if (
        before.mode != "native"
        or before.write_epoch != 0
        or not before.v1_fallback_open
        or before.write_enabled
    ):
        raise LauncherRaceError(
            "installed launcher baseline requires the epoch-zero seed"
        )
    installed = _validated_install_roots(install_roots)
    layout_sha256 = _install_layout_hashes(installed)
    if len(set(layout_sha256.values())) != 1:
        raise LauncherRaceError("source and packaged launcher layouts do not match")
    wave = _run_installed_wave(
        installed,
        root / "reports.db",
        timeout_seconds=process_timeout_seconds,
    )
    _require_wave_identity(wave, _runtime_identity(before), "before")
    return {
        "schema_version": 1,
        "kind": "v2_epoch_zero_installed_launcher_baseline",
        "runtime_identity": _redacted_identity(before),
        "launcher_layout_sha256": layout_sha256,
        "wave": wave,
    }


def _validated_install_roots(
    install_roots: Mapping[str, str | Path],
) -> dict[str, Path]:
    if set(install_roots) != set(_INSTALL_LABELS) or len(install_roots) != len(
        _INSTALL_LABELS
    ):
        raise LauncherRaceError(
            "launcher race requires source-default and packaged-default install roots"
        )
    validated: dict[str, Path] = {}
    for label in _INSTALL_LABELS:
        raw_root = Path(install_roots[label])
        if raw_root.is_symlink():
            raise LauncherRaceError("launcher race install root cannot be a symlink")
        try:
            root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise LauncherRaceError("launcher race install root is unavailable") from exc
        if not root.is_dir():
            raise LauncherRaceError("launcher race install root is unavailable")
        for relative in _LAUNCHER_LAYOUT:
            candidate = root.joinpath(*relative.split("/"))
            if not candidate.is_file() or candidate.is_symlink():
                raise LauncherRaceError(
                    f"launcher race install root is missing required layout: {relative}"
                )
        python = root / ".venv" / "Scripts" / "python.exe"
        if not python.is_file() or python.is_symlink():
            raise LauncherRaceError(
                "launcher race install root requires its own .venv Python"
            )
        validated[label] = root
    if validated["source-default"] == validated["packaged-default"]:
        raise LauncherRaceError(
            "source-default and packaged-default require distinct install roots"
        )
    return validated


def _validated_installed_baseline(
    baseline: Mapping[str, Any],
    install_roots: Mapping[str, Path],
    expected_identity: tuple[Any, ...],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if (
        baseline.get("schema_version") != 1
        or baseline.get("kind") != "v2_epoch_zero_installed_launcher_baseline"
    ):
        raise LauncherRaceError("installed launcher baseline schema is invalid")
    identity = baseline.get("runtime_identity")
    if not isinstance(identity, Mapping) or _payload_identity(identity) != expected_identity:
        raise LauncherRaceError("installed launcher baseline identity is stale")
    supplied_hashes = baseline.get("launcher_layout_sha256")
    if not isinstance(supplied_hashes, Mapping) or set(supplied_hashes) != set(
        _INSTALL_LABELS
    ):
        raise LauncherRaceError("installed launcher baseline layout is invalid")
    layout_sha256 = {label: supplied_hashes.get(label) for label in _INSTALL_LABELS}
    if any(not _is_sha256(value) for value in layout_sha256.values()) or len(
        set(layout_sha256.values())
    ) != 1:
        raise LauncherRaceError("installed launcher baseline layout is invalid")
    if _install_layout_hashes(install_roots) != layout_sha256:
        raise LauncherRaceError("installed launcher baseline layout is stale")
    supplied_wave = baseline.get("wave")
    if not isinstance(supplied_wave, list) or any(
        not isinstance(outcome, dict) for outcome in supplied_wave
    ):
        raise LauncherRaceError("installed launcher baseline wave is invalid")
    wave = json.loads(canonical_json(supplied_wave))
    _require_wave_identity(wave, expected_identity, "before")
    return wave, layout_sha256


def _install_layout_hashes(install_roots: Mapping[str, Path]) -> dict[str, str]:
    return {
        label: _launcher_layout_sha256(install_roots[label])
        for label in _INSTALL_LABELS
    }


def _launcher_layout_sha256(root: Path) -> str:
    files = [
        root / "quickstart.py",
        root / "RUN_APP.bat",
        root / "RUN_QUICKSTART.bat",
        root / "MIGRATE_V2.bat",
        root / "REBUILD_V2.bat",
        root / "scripts" / "migrations" / "v2" / "migrate_v2_user.py",
        root / "scripts" / "migrations" / "v2" / "rebuild_v2_successor.py",
        *sorted((root / "apps").rglob("*.py")),
        *sorted((root / "src").rglob("*.py")),
    ]
    digest = hashlib.sha256()
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise LauncherRaceError(
                "launcher install layout contains an unavailable or unsafe file"
            )
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path)))
        digest.update(b"\n")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


def _surface_commands(root: Path) -> tuple[tuple[str, list[str]], ...]:
    python = root / ".venv" / "Scripts" / "python.exe"
    return (
        ("launcher_guard", [str(python), "-m", "src.retrieval.launcher_guard"]),
        (
            "update_guard",
            [str(python), "-m", "src.retrieval.launcher_guard", "--write"],
        ),
        ("gui", [str(python), "apps/gui/app.py", "--runtime-smoke"]),
        ("cli", [str(python), "-m", "apps.cli.app", "--runtime-smoke"]),
        ("quick_start", [str(python), "quickstart.py", "--runtime-smoke"]),
        (
            "run_app_bat",
            ["cmd", "/d", "/c", "RUN_APP.bat", "--runtime-smoke"],
        ),
        (
            "run_quickstart_bat",
            ["cmd", "/d", "/c", "RUN_QUICKSTART.bat", "--runtime-smoke"],
        ),
    )


def _start_installed_wave(
    install_roots: Mapping[str, Path],
    legacy_anchor: Path,
) -> list[dict[str, Any]]:
    return [
        _start_installed_process(label, root, surface, command, legacy_anchor)
        for label in _INSTALL_LABELS
        for root in (install_roots[label],)
        for surface, command in _surface_commands(root)
    ]


def _start_installed_process(
    label: str,
    root: Path,
    surface: str,
    command: list[str],
    legacy_anchor: Path,
) -> dict[str, Any]:
    python = root / ".venv" / "Scripts" / "python.exe"
    environment = os.environ.copy()
    environment["DB_PATH"] = str(legacy_anchor)
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get(
        "PATH", ""
    )
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    started_ns = time.perf_counter_ns()
    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        process = None
    return {
        "label": label,
        "surface": surface,
        "started_ns": started_ns,
        "process": process,
    }


def _run_installed_wave(
    install_roots: Mapping[str, Path],
    legacy_anchor: Path,
    *,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    # Baseline and post-cutover waves are deliberately serial: every launcher
    # performs startup reconciliation under the same single-writer lock, so a
    # concurrent baseline would measure lock contention instead of identity.
    outcomes: list[dict[str, Any]] = []
    for label in _INSTALL_LABELS:
        root = install_roots[label]
        for surface, command in _surface_commands(root):
            outcomes.extend(
                _finish_installed_wave(
                    [
                        _start_installed_process(
                            label,
                            root,
                            surface,
                            command,
                            legacy_anchor,
                        )
                    ],
                    timeout_seconds=timeout_seconds,
                )
            )
    return outcomes


def _finish_installed_wave(
    processes: list[dict[str, Any]],
    *,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout_seconds
    for record in processes:
        process = record["process"]
        if process is None:
            outcomes.append(
                _process_outcome(record, None, b"", "unavailable")
            )
            continue
        remaining = max(0.001, deadline - time.monotonic())
        try:
            stdout, stderr = process.communicate(timeout=remaining)
            outcomes.append(
                _process_outcome(
                    record,
                    process.returncode,
                    (stdout or b"") + (stderr or b""),
                    None,
                )
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            combined = (
                (exc.stdout or b"")
                + (exc.stderr or b"")
                + (stdout or b"")
                + (stderr or b"")
            )
            outcomes.append(_process_outcome(record, None, combined, "timeout"))
    return outcomes


def _process_outcome(
    record: Mapping[str, Any],
    exit_code: int | None,
    output: bytes,
    error_code: str | None,
) -> dict[str, Any]:
    payload = _last_status_payload(output)
    identity = None if payload is None else _payload_identity(payload)
    if exit_code == 0 and identity is not None and payload.get("status") == "ok":
        disposition = "selected"
    elif exit_code is not None and exit_code != 0:
        disposition = "blocked"
    else:
        disposition = "invalid"
    return {
        "label": record["label"],
        "surface": record["surface"],
        "exit_code": exit_code,
        "duration_ns": time.perf_counter_ns() - int(record["started_ns"]),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "disposition": disposition,
        "runtime_identity": (
            None if identity is None else _identity_mapping(identity)
        ),
        **({} if error_code is None else {"error_code": error_code}),
    }


def _last_status_payload(output: bytes) -> dict[str, Any] | None:
    payload = None
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "status" in value:
            payload = value
    return payload


def _payload_identity(payload: Mapping[str, Any]) -> tuple[Any, ...] | None:
    snapshot_id = payload.get("active_snapshot_id")
    generation = payload.get("publication_generation")
    epoch = payload.get("write_epoch")
    boolean_fields = (
        payload.get("v1_fallback_open"),
        payload.get("degraded"),
        payload.get("write_enabled"),
    )
    if (
        payload.get("mode") != "native"
        or not isinstance(snapshot_id, str)
        or not snapshot_id
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 0
        or any(not isinstance(value, bool) for value in boolean_fields)
    ):
        return None
    return (
        "native",
        snapshot_id,
        generation,
        epoch,
        *boolean_fields,
    )


def _identity_mapping(identity: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "mode": identity[0],
        "active_snapshot_id": identity[1],
        "publication_generation": identity[2],
        "write_epoch": identity[3],
        "v1_fallback_open": identity[4],
        "degraded": identity[5],
        "write_enabled": identity[6],
    }


def _outcome_identity(outcome: Mapping[str, Any]) -> tuple[Any, ...] | None:
    identity = outcome.get("runtime_identity")
    return None if not isinstance(identity, Mapping) else _payload_identity(identity)


def _require_wave_identity(
    wave: list[dict[str, Any]],
    expected: tuple[Any, ...],
    phase: str,
) -> None:
    _require_complete_wave(wave)
    for outcome in wave:
        if phase == "before" and outcome["surface"] == "update_guard":
            if outcome["disposition"] != "blocked":
                raise LauncherRaceError(
                    "epoch-zero installed updater did not fail closed"
                )
            continue
        if (
            outcome["disposition"] != "selected"
            or _outcome_identity(outcome) != expected
        ):
            raise LauncherRaceError(
                f"installed launcher did not select the {phase} runtime"
            )


def _require_wave_safe_during_transition(
    wave: list[dict[str, Any]],
    allowed: set[tuple[Any, ...]],
) -> None:
    _require_complete_wave(wave)
    before, after = sorted(allowed, key=lambda identity: int(identity[3]))
    for outcome in wave:
        if outcome["disposition"] == "blocked":
            continue
        identity = _outcome_identity(outcome)
        if outcome["disposition"] != "selected" or identity not in allowed:
            raise LauncherRaceError(
                "installed launcher observed an unsafe transition state"
            )
        if outcome["surface"] == "update_guard" and identity == before:
            raise LauncherRaceError(
                "installed updater became writable before the successor"
            )
    if not any(
        outcome["surface"] == "update_guard"
        for outcome in wave
    ) or not any(
        outcome["surface"] != "update_guard"
        for outcome in wave
    ):
        raise LauncherRaceError("installed transition wave missed a guarded surface")


def _require_wave_safe_at_boundary(
    wave: list[dict[str, Any]],
    expected: tuple[Any, ...],
) -> None:
    _require_complete_wave(wave)
    selected = 0
    for outcome in wave:
        if outcome["disposition"] == "blocked":
            continue
        if (
            outcome["disposition"] != "selected"
            or _outcome_identity(outcome) != expected
        ):
            raise LauncherRaceError(
                "concurrent installed launcher observed an unsafe boundary state"
            )
        selected += 1
    if selected == 0:
        raise LauncherRaceError(
            "concurrent installed launcher wave did not select the boundary runtime"
        )


def _require_complete_wave(wave: list[dict[str, Any]]) -> None:
    expected = {
        (label, surface)
        for label in _INSTALL_LABELS
        for surface, _command in _surface_commands(Path("."))
    }
    observed = {(outcome.get("label"), outcome.get("surface")) for outcome in wave}
    if observed != expected or len(wave) != len(expected):
        raise LauncherRaceError("installed launcher wave is incomplete")


def _runtime_identity(selection: RuntimeSelection) -> tuple[Any, ...]:
    return (
        selection.mode,
        selection.active_snapshot_id,
        selection.publication_generation,
        selection.write_epoch,
        selection.v1_fallback_open,
        selection.degraded,
        selection.write_enabled,
    )


def _redacted_identity(selection: RuntimeSelection) -> dict[str, Any]:
    return {
        "mode": selection.mode,
        "active_snapshot_id": selection.active_snapshot_id,
        "publication_generation": selection.publication_generation,
        "write_epoch": selection.write_epoch,
        "v1_fallback_open": selection.v1_fallback_open,
        "degraded": selection.degraded,
        "write_enabled": selection.write_enabled,
    }


__all__ = [
    "LauncherRaceError",
    "LauncherRaceResult",
    "capture_epoch_zero_installed_baseline",
    "publish_candidate_with_launcher_race",
]
