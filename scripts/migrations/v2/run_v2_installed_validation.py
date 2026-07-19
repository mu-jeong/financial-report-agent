"""Run one installed V2 release validation and seal immutable evidence."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.migrations.v2.validation.installed_validation import (
    INSTALL_LABELS,
    REQUIRED_DISTRIBUTIONS,
)
from src.migrations.v2.validation.launcher_race import (
    _install_layout_hashes,
    _validated_install_roots,
)
from src.migrations.v2.validation.performance import REQUIRED_WORKLOADS
from src.migrations.v2.validation.release_transitions import (
    capture_tree_manifest,
    execute_release_transitions,
    validate_release_transition_evidence,
)
from src.retrieval.bootstrap import RuntimeSelection, inspect_runtime
from src.retrieval.identity import canonical_json


MAXIMUM_TIMEOUT_SECONDS = 5 * 60


class InstalledValidationRunError(RuntimeError):
    """Raised when installed release behavior cannot be proved."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one installed V2 retrieval release validation"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--protected-root", type=Path, required=True)
    parser.add_argument("--source-pdfs", type=Path, required=True)
    parser.add_argument("--query-spec", type=Path, required=True)
    parser.add_argument("--source-install", type=Path, required=True)
    parser.add_argument("--packaged-install", type=Path, required=True)
    parser.add_argument("--transition-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--search-samples", type=int, default=20)
    args = parser.parse_args(argv)
    if (
        args.timeout_seconds <= 0
        or args.timeout_seconds > MAXIMUM_TIMEOUT_SECONDS
    ):
        parser.error("--timeout-seconds must be between 1 and 300")
    if args.search_samples < 5:
        parser.error("--search-samples must be at least 5")

    output_path = args.output.absolute()
    transition_path = args.transition_output.absolute()
    writable_paths_validated = False
    try:
        data_root = _validate_dedicated_root(args.data_root, args.protected_root)
        protected_root = args.protected_root.resolve(strict=True)
        source_pdfs = args.source_pdfs.resolve(strict=True)
        query_spec = args.query_spec.resolve(strict=True)
        if not source_pdfs.is_dir() or source_pdfs.is_symlink():
            raise InstalledValidationRunError(
                "source PDF corpus is unavailable or unsafe"
            )
        if not query_spec.is_file() or query_spec.is_symlink():
            raise InstalledValidationRunError(
                "semantic query specification is unavailable or unsafe"
            )
        installs = _validated_install_roots(
            {
                "source-default": args.source_install,
                "packaged-default": args.packaged_install,
            }
        )
        writable_paths = _validate_writable_paths(
            {"output": output_path, "transition": transition_path},
            {
                "dedicated": data_root,
                "protected": protected_root,
                "source-pdfs": source_pdfs,
                **installs,
            },
        )
        output_path = writable_paths["output"]
        transition_path = writable_paths["transition"]
        _validate_distinct_paths(
            {**writable_paths, "query": query_spec}
        )
        writable_paths_validated = True
        if os.name != "nt" or _is_admin():
            raise InstalledValidationRunError(
                "installed validation requires non-admin Windows execution"
            )
        if output_path.exists() or transition_path.exists():
            raise InstalledValidationRunError(
                "installed validation evidence paths must be new"
            )

        layout_hashes = _install_layout_hashes(installs)
        if set(layout_hashes) != set(INSTALL_LABELS) or len(
            set(layout_hashes.values())
        ) != 1:
            raise InstalledValidationRunError(
                "source and packaged launcher layouts differ"
            )
        layout_hash = next(iter(layout_hashes.values()))
        installed_environments = _capture_installed_environments(
            installs,
            timeout_seconds=args.timeout_seconds,
        )
        installed_environments_sha256 = hashlib.sha256(
            canonical_json(installed_environments).encode("utf-8")
        ).hexdigest()
        started_at = _utc_now()

        transition_payload = execute_release_transitions(
            data_root,
            protected_root,
            source_pdfs,
            query_spec,
        )
        transition_summary = validate_release_transition_evidence(
            transition_payload
        )
        _write_immutable_json(transition_path, transition_payload)
        transition_value = _read_object(transition_path)
        sealed_summary = validate_release_transition_evidence(transition_value)
        if sealed_summary != transition_summary:
            raise InstalledValidationRunError(
                "sealed transition evidence changed during publication"
            )

        retained_paths = {"query": query_spec, "transition": transition_path}
        retained_hashes = {
            name: _sha256_file(path) for name, path in retained_paths.items()
        }
        if retained_hashes["query"] != transition_summary["query_spec_sha256"]:
            raise InstalledValidationRunError(
                "transition evidence is not bound to the supplied query"
            )
        baseline = _capture_baseline(data_root)
        baseline_transition_identity = {
            field: baseline["runtime_identity"][field]
            for field in (
                "active_snapshot_id",
                "predecessor_snapshot_id",
                "publication_generation",
                "write_epoch",
                "v1_fallback_open",
                "degraded",
                "write_enabled",
            )
        }
        if (
            baseline_transition_identity
            != transition_summary["final_runtime_identity"]
        ):
            raise InstalledValidationRunError(
                "transition final identity differs from the validation root"
            )

        validation = _run_validation(
            data_root=data_root,
            installs=installs,
            baseline=baseline,
            timeout_seconds=args.timeout_seconds,
            search_samples=args.search_samples,
            expected_layout_hash=layout_hash,
            query_spec=query_spec,
        )
        final = _capture_baseline(data_root)
        _validate_install_layouts(installs, layout_hash)
        _validate_installed_environments(
            installs,
            installed_environments,
            timeout_seconds=args.timeout_seconds,
        )
        _validate_artifact_hashes(retained_paths, retained_hashes)
        protected_tree_sha256 = capture_tree_manifest(protected_root)["sha256"]
        source_tree_sha256 = capture_tree_manifest(source_pdfs)["sha256"]
        if (
            protected_tree_sha256
            != transition_summary["protected_tree_sha256_after"]
        ):
            raise InstalledValidationRunError(
                "protected root changed during installed validation"
            )
        if source_tree_sha256 != transition_summary["source_tree_sha256_after"]:
            raise InstalledValidationRunError(
                "source PDF corpus changed during installed validation"
            )
        if final != baseline:
            raise InstalledValidationRunError(
                "runtime or immutable artifact changed during installed validation"
            )

        completed_at = _utc_now()
        payload = {
            "schema_version": 1,
            "kind": "v2_installed_validation",
            "passed": True,
            "fixture_only": False,
            "release_eligible": False,
            "release_gate_pending": "aggregate_release_gate_manifest",
            "started_at": started_at,
            "completed_at": completed_at,
            "environment": {
                "os": platform.system().lower(),
                "os_release": platform.release(),
                "python_version": platform.python_version(),
                "non_admin": True,
            },
            "timeout_seconds": args.timeout_seconds,
            "search_samples": args.search_samples,
            "launcher_layout_sha256": layout_hash,
            "installed_environments": installed_environments,
            "installed_environments_sha256": installed_environments_sha256,
            "retained_evidence": retained_hashes,
            "transition_run_id": transition_summary["run_id"],
            "protected_tree_sha256": protected_tree_sha256,
            "source_tree_sha256": source_tree_sha256,
            "baseline": baseline,
            "final": final,
            "validation": validation,
        }
        _write_immutable_json(output_path, payload)
        print(
            json.dumps(
                {
                    "status": "installed_validation_passed_release_gate_pending",
                    "evidence": output_path.name,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "kind": "v2_installed_validation_failure",
            "passed": False,
            "release_eligible": False,
            "error": type(exc).__name__,
        }
        if writable_paths_validated and not output_path.exists():
            try:
                _write_immutable_json(output_path, failure)
            except (OSError, InstalledValidationRunError):
                pass
        print(
            json.dumps(
                {"status": "failed", "evidence": output_path.name},
                ensure_ascii=False,
            )
        )
        return 1


def _run_validation(
    *,
    data_root: Path,
    installs: Mapping[str, Path],
    baseline: Mapping[str, Any],
    timeout_seconds: int,
    search_samples: int,
    expected_layout_hash: str,
    query_spec: Path,
) -> dict[str, Any]:
    _validate_install_layouts(installs, expected_layout_hash)
    anchor = data_root / "reports.db"
    outcomes = [
        _run_guard(
            label,
            installs[label],
            anchor,
            write=write,
            timeout=timeout_seconds,
        )
        for label in INSTALL_LABELS
        for write in (False, True)
    ]
    expected_identity = baseline["runtime_identity"]
    if any(outcome["runtime_identity"] != expected_identity for outcome in outcomes):
        raise InstalledValidationRunError(
            "installed guard selected a different runtime identity"
        )
    search_probes = [
        _run_installed_probe(
            label,
            installs[label],
            data_root,
            query_spec,
            samples=search_samples,
            timeout=timeout_seconds,
        )
        for label in INSTALL_LABELS
    ]
    for probe in search_probes:
        probe_identity = probe.get("runtime_identity")
        if not isinstance(probe_identity, Mapping) or (
            probe_identity.get("active_snapshot_id")
            != expected_identity["active_snapshot_id"]
            or probe_identity.get("publication_generation")
            != expected_identity["publication_generation"]
        ):
            raise InstalledValidationRunError(
                "installed search probe selected a different runtime"
            )
    current = _capture_baseline(data_root)
    if current != baseline:
        raise InstalledValidationRunError(
            "installed validation changed runtime or immutable artifacts"
        )
    return {
        "recorded_at": _utc_now(),
        "passed": True,
        "guard_outcomes": outcomes,
        "search_probes": search_probes,
        "runtime_identity_sha256": hashlib.sha256(
            canonical_json(expected_identity).encode("utf-8")
        ).hexdigest(),
        "catalog_sha256": baseline["catalog_sha256"],
        "snapshot_sha256": baseline["snapshot_sha256"],
    }


def _run_installed_probe(
    label: str,
    install_root: Path,
    data_root: Path,
    query_spec: Path,
    *,
    samples: int,
    timeout: int,
) -> dict[str, Any]:
    python = install_root / ".venv" / "Scripts" / "python.exe"
    command = [
        str(python),
        "-m",
        "src.migrations.v2.validation.installed_probe",
        "--data-root",
        str(data_root),
        "--query-spec",
        str(query_spec),
        "--samples",
        str(samples),
    ]
    environment = _subprocess_environment(python)
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            command,
            cwd=install_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstalledValidationRunError(
            "installed semantic search probe failed"
        ) from exc
    output = (completed.stdout or b"") + (completed.stderr or b"")
    payload = _last_status_payload(output)
    if (
        completed.returncode != 0
        or payload is None
        or payload.get("status") != "ok"
        or payload.get("kind") != "v2_installed_validation_probe"
        or payload.get("passed") is not True
    ):
        raise InstalledValidationRunError(
            "installed semantic search probe failed"
        )
    workloads = payload.get("workloads")
    gate_d = payload.get("gate_d_search")
    if (
        not isinstance(workloads, Mapping)
        or set(workloads) != set(REQUIRED_WORKLOADS)
        or not isinstance(gate_d, Mapping)
        or gate_d.get("citation_complete") is not True
        or gate_d.get("top_rank") != 1
    ):
        raise InstalledValidationRunError(
            "installed semantic search probe evidence is incomplete"
        )
    result = dict(payload)
    result.pop("status", None)
    result["install"] = label
    result["duration_ns"] = time.perf_counter_ns() - started
    result["output_sha256"] = hashlib.sha256(output).hexdigest()
    return result


def _run_guard(
    label: str,
    install_root: Path,
    anchor: Path,
    *,
    write: bool,
    timeout: int,
) -> dict[str, Any]:
    python = install_root / ".venv" / "Scripts" / "python.exe"
    command = [str(python), "-m", "src.retrieval.launcher_guard"]
    if write:
        command.append("--write")
    environment = _subprocess_environment(python)
    environment["DB_PATH"] = str(anchor)
    started = time.perf_counter_ns()
    try:
        completed = subprocess.run(
            command,
            cwd=install_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstalledValidationRunError("installed guard failed") from exc
    output = (completed.stdout or b"") + (completed.stderr or b"")
    payload = _last_status_payload(output)
    if completed.returncode != 0 or payload is None or payload.get("status") != "ok":
        raise InstalledValidationRunError("installed guard failed closed")
    identity = _payload_identity(payload)
    if identity is None:
        raise InstalledValidationRunError(
            "installed guard returned an invalid runtime identity"
        )
    return {
        "install": label,
        "write_guard": write,
        "duration_ns": time.perf_counter_ns() - started,
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "runtime_identity": identity,
    }


def _capture_baseline(data_root: Path) -> dict[str, Any]:
    selection = inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=True,
    )
    if (
        selection.mode != "native"
        or selection.write_epoch <= 0
        or selection.v1_fallback_open
        or selection.degraded
        or not selection.write_enabled
        or selection.predecessor_snapshot_id is None
        or selection.predecessor_snapshot_id == selection.active_snapshot_id
    ):
        raise InstalledValidationRunError(
            "validation requires a healthy writable two-snapshot runtime"
        )
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(
        f"file:{catalog.as_posix()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        rows = {
            role: connection.execute(
                """
                SELECT relative_path, file_sha256, size_bytes, dimension,
                       metric, ntotal, state
                FROM vector_snapshots WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
            for role, snapshot_id in (
                ("active", selection.active_snapshot_id),
                ("predecessor", selection.predecessor_snapshot_id),
            )
        }
    finally:
        connection.close()
    snapshots: dict[str, Any] = {}
    for role, row in rows.items():
        if row is None or row[6] != "ready":
            raise InstalledValidationRunError(
                f"{role} snapshot descriptor is missing or not ready"
            )
        snapshot = data_root.joinpath(*str(row[0]).split("/"))
        if not snapshot.is_file() or snapshot.is_symlink():
            raise InstalledValidationRunError(
                f"{role} snapshot path is unavailable or unsafe"
            )
        if _sha256_file(snapshot) != row[1]:
            raise InstalledValidationRunError(f"{role} snapshot hash changed")
        snapshots[role] = {
            "snapshot_id": (
                selection.active_snapshot_id
                if role == "active"
                else selection.predecessor_snapshot_id
            ),
            "relative_path": str(row[0]),
            "sha256": str(row[1]),
            "size_bytes": int(row[2]),
            "dimension": int(row[3]),
            "metric": str(row[4]),
            "ntotal": int(row[5]),
        }
    writer_lock = data_root / "retrieval" / "v2" / "writer.lock"
    staging = data_root / "retrieval" / "v2" / "staging"
    if writer_lock.exists() or any(staging.iterdir()):
        raise InstalledValidationRunError("writer or staging residue is present")
    return {
        "runtime_identity": _runtime_identity(selection),
        "catalog_sha256": _sha256_file(catalog),
        "catalog_logical_sha256": _catalog_logical_sha256(catalog),
        "snapshot_sha256": snapshots["active"]["sha256"],
        "snapshots": snapshots,
        "writer_lock": False,
        "staging_entries": 0,
    }


def _capture_installed_environments(
    installs: Mapping[str, Path],
    *,
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    code = "\n".join(
        (
            "import importlib.metadata as metadata",
            "import json, platform",
            f"names = {list(REQUIRED_DISTRIBUTIONS)!r}",
            "versions = {}",
            "for name in names:",
            "    try:",
            "        versions[name] = metadata.version(name)",
            "    except metadata.PackageNotFoundError:",
            "        versions[name] = None",
            "print(json.dumps({'status': 'ok', 'python_version': platform.python_version(), 'implementation': platform.python_implementation(), 'packages': versions}, sort_keys=True))",
        )
    )
    results: dict[str, dict[str, Any]] = {}
    for label, root in installs.items():
        python = root / ".venv" / "Scripts" / "python.exe"
        try:
            completed = subprocess.run(
                [str(python), "-I", "-c", code],
                cwd=root,
                env=_subprocess_environment(python),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstalledValidationRunError(
                "installed environment probe failed"
            ) from exc
        payload = _last_status_payload(
            (completed.stdout or b"") + (completed.stderr or b"")
        )
        packages = payload.get("packages") if isinstance(payload, Mapping) else None
        if (
            completed.returncode != 0
            or not isinstance(payload, Mapping)
            or payload.get("status") != "ok"
            or not isinstance(payload.get("python_version"), str)
            or not payload.get("python_version")
            or payload.get("implementation") != "CPython"
            or not isinstance(packages, Mapping)
            or set(packages) != set(REQUIRED_DISTRIBUTIONS)
            or any(
                not isinstance(packages[name], str) or not packages[name]
                for name in packages
            )
        ):
            raise InstalledValidationRunError(
                "installed environment fingerprint is incomplete"
            )
        semantic = {
            "python_version": payload["python_version"],
            "implementation": payload["implementation"],
            "packages": dict(sorted(packages.items())),
        }
        results[label] = {
            **semantic,
            "python_executable_sha256": _sha256_file(python),
            "semantic_sha256": hashlib.sha256(
                canonical_json(semantic).encode("utf-8")
            ).hexdigest(),
        }
    semantic_hashes = {value["semantic_sha256"] for value in results.values()}
    executable_hashes = {
        value["python_executable_sha256"] for value in results.values()
    }
    if (
        set(results) != set(INSTALL_LABELS)
        or len(semantic_hashes) != 1
        or len(executable_hashes) != 1
    ):
        raise InstalledValidationRunError(
            "source and packaged dependency environments differ"
        )
    return results


def _validate_installed_environments(
    installs: Mapping[str, Path],
    expected: Mapping[str, Mapping[str, Any]],
    *,
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    current = _capture_installed_environments(
        installs,
        timeout_seconds=timeout_seconds,
    )
    if current != {label: dict(value) for label, value in expected.items()}:
        raise InstalledValidationRunError(
            "installed interpreter or dependency fingerprint changed"
        )
    return current


def _validate_install_layouts(
    installs: Mapping[str, Path],
    expected_hash: str,
) -> dict[str, str]:
    current = _install_layout_hashes(installs)
    if set(current) != set(installs) or any(
        value != expected_hash for value in current.values()
    ):
        raise InstalledValidationRunError(
            "source or packaged launcher layout changed"
        )
    return current


def _validate_artifact_hashes(
    paths: Mapping[str, Path],
    expected: Mapping[str, str],
) -> dict[str, str]:
    if set(paths) != set(expected):
        raise InstalledValidationRunError("retained artifact roles changed")
    current = {name: _sha256_file(path) for name, path in paths.items()}
    if current != dict(expected):
        raise InstalledValidationRunError("retained artifact hash changed")
    return current


def _validate_distinct_paths(paths: Mapping[str, Path]) -> dict[str, Path]:
    resolved = {name: path.resolve(strict=False) for name, path in paths.items()}
    normalized = [os.path.normcase(str(path)) for path in resolved.values()]
    if len(normalized) != len(set(normalized)):
        raise InstalledValidationRunError(
            "validation output and input paths must be distinct"
        )
    return resolved


def _validate_writable_paths(
    paths: Mapping[str, Path],
    protected_roots: Mapping[str, Path],
) -> dict[str, Path]:
    resolved: dict[str, Path] = {}
    for name, path in paths.items():
        if _path_has_reparse_component(path):
            raise InstalledValidationRunError(f"{name} output path is unsafe")
        resolved[name] = path.resolve(strict=False)
    _validate_distinct_paths(resolved)
    for name, path in resolved.items():
        for root_name, root in protected_roots.items():
            protected = root.resolve(strict=True)
            if _paths_overlap(path, protected):
                raise InstalledValidationRunError(
                    f"{name} output must remain outside the {root_name} root"
                )
    return resolved


def _validate_dedicated_root(data_root: Path, protected_root: Path) -> Path:
    if _path_has_reparse_component(data_root):
        raise InstalledValidationRunError(
            "dedicated data root must not contain a symlink or junction"
        )
    if _path_has_reparse_component(protected_root):
        raise InstalledValidationRunError(
            "protected data root must not contain a symlink or junction"
        )
    dedicated = data_root.resolve(strict=True)
    protected = protected_root.resolve(strict=True)
    if not dedicated.is_dir() or not protected.is_dir() or dedicated == protected:
        raise InstalledValidationRunError(
            "validation requires a dedicated copied data root"
        )
    return dedicated


def _catalog_logical_sha256(catalog: Path) -> str:
    connection = sqlite3.connect(
        f"file:{catalog.resolve(strict=True).as_posix()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        connection.execute("BEGIN")
        schema = [
            list(row)
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
        ]
        table_names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        tables: dict[str, Any] = {}
        for name in table_names:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({quoted})"
                ).fetchall()
            ]
            rows = [
                [_json_sql_value(item) for item in row]
                for row in connection.execute(f"SELECT * FROM {quoted}").fetchall()
            ]
            rows.sort(key=canonical_json)
            tables[name] = {"columns": columns, "rows": rows}
        payload = {"schema": schema, "tables": tables}
        return hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
    finally:
        connection.close()


def _json_sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise InstalledValidationRunError(
        "catalog contains an unsupported SQLite value"
    )


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


def _payload_identity(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    snapshot = payload.get("active_snapshot_id")
    predecessor = payload.get("predecessor_snapshot_id")
    generation = payload.get("publication_generation")
    epoch = payload.get("write_epoch")
    booleans = (
        payload.get("v1_fallback_open"),
        payload.get("degraded"),
        payload.get("write_enabled"),
    )
    if (
        payload.get("mode") != "native"
        or not isinstance(snapshot, str)
        or not snapshot
        or not isinstance(predecessor, str)
        or not predecessor
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch <= 0
        or any(not isinstance(value, bool) for value in booleans)
    ):
        return None
    return {
        "mode": "native",
        "active_snapshot_id": snapshot,
        "predecessor_snapshot_id": predecessor,
        "publication_generation": generation,
        "write_epoch": epoch,
        "v1_fallback_open": booleans[0],
        "degraded": booleans[1],
        "write_enabled": booleans[2],
    }


def _runtime_identity(selection: RuntimeSelection) -> dict[str, Any]:
    return {
        "mode": selection.mode,
        "active_snapshot_id": selection.active_snapshot_id,
        "predecessor_snapshot_id": selection.predecessor_snapshot_id,
        "publication_generation": selection.publication_generation,
        "write_epoch": selection.write_epoch,
        "v1_fallback_open": selection.v1_fallback_open,
        "degraded": selection.degraded,
        "write_enabled": selection.write_enabled,
    }


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise InstalledValidationRunError("evidence is unavailable or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstalledValidationRunError("evidence is unreadable") from exc
    if not isinstance(value, dict):
        raise InstalledValidationRunError("evidence must be an object")
    return value


def _write_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise InstalledValidationRunError("evidence path already exists")
    path = _prepare_output_target(path)
    if path.exists() or path.is_symlink():
        raise InstalledValidationRunError("evidence path already exists")
    encoded = (canonical_json(dict(payload)) + "\n").encode("utf-8")
    temporary = path.parent / f".installed-validation-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        os.chmod(path, stat.S_IREAD)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_output_target(path: Path) -> Path:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _path_has_reparse_component(path.parent):
            raise InstalledValidationRunError("output parent is unsafe")
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise InstalledValidationRunError("output parent is unavailable") from exc
    target = parent / path.name
    if (
        not path.name
        or os.path.normcase(str(target))
        != os.path.normcase(str(path.resolve(strict=False)))
    ):
        raise InstalledValidationRunError(
            "output path changed during publication"
        )
    return target


def _subprocess_environment(python: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get(
        "PATH", ""
    )
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _paths_overlap(left: Path, right: Path) -> bool:
    left_value = os.path.normcase(str(left))
    right_value = os.path.normcase(str(right))
    try:
        common = os.path.commonpath((left_value, right_value))
    except ValueError:
        return False
    return common in {left_value, right_value}


def _path_has_reparse_component(path: Path) -> bool:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if component.is_symlink() or attributes & reparse_point:
            return True
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    raise SystemExit(main())
