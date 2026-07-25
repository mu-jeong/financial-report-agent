"""Execute non-interactive retrieval startup smoke checks for every launcher."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.retrieval.identity import canonical_json


_LABEL = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_REQUIRED_CASE_LABELS = (
    "source-default",
    "packaged-default",
    "custom-local",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the V2 retrieval launcher smoke matrix without opening a UI"
    )
    parser.add_argument(
        "--case",
        action="append",
        required=True,
        metavar="LABEL=DB_PATH",
        help="redacted case label and reports.db anchor path",
    )
    parser.add_argument(
        "--install-root",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="case label and the independently installed launcher root",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument(
        "--require-non-admin",
        action="store_true",
        help="retained for explicit operator intent; non-admin execution is always required",
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    parsed_cases = [_parse_case(value) for value in args.case]
    parsed_install_roots = [
        _parse_install_root(value) for value in args.install_root
    ]
    labels = [label for label, _path in parsed_cases]
    install_labels = [label for label, _path in parsed_install_roots]
    if len(labels) != len(set(labels)):
        parser.error("launcher case labels must be unique")
    if len(install_labels) != len(set(install_labels)):
        parser.error("launcher install-root labels must be unique")
    if set(labels) != set(_REQUIRED_CASE_LABELS) or len(labels) != len(
        _REQUIRED_CASE_LABELS
    ):
        parser.error(
            "launcher matrix requires exactly source-default, "
            "packaged-default, and custom-local"
        )
    if set(install_labels) != set(_REQUIRED_CASE_LABELS) or len(
        install_labels
    ) != len(_REQUIRED_CASE_LABELS):
        parser.error(
            "launcher matrix requires one install root for every required case"
        )
    by_label = dict(parsed_cases)
    install_by_label = dict(parsed_install_roots)
    cases = [
        (label, install_by_label[label], by_label[label])
        for label in _REQUIRED_CASE_LABELS
    ]
    if len({path for _label, _root, path in cases}) != len(cases):
        parser.error("launcher matrix cases must use distinct data roots")
    if install_by_label["source-default"] == install_by_label["packaged-default"]:
        parser.error(
            "source-default and packaged-default require distinct install roots"
        )
    custom_path = str(by_label["custom-local"])
    if not any(character.isspace() for character in custom_path) or not re.search(
        r"[\uac00-\ud7a3]", custom_path
    ):
        parser.error("custom-local path must contain both whitespace and Korean text")
    non_admin = not _is_admin()
    results = [
        _run_case(
            label,
            install_root,
            db_path,
            timeout_seconds=args.timeout_seconds,
        )
        for label, install_root, db_path in cases
    ]
    runtime_identities = {
        canonical_json(result["runtime_identity"])
        for result in results
        if result["runtime_identity"] is not None
    }
    catalog_hashes = {result["catalog_sha256"] for result in results}
    launcher_layout_hashes = {
        result["launcher_layout_sha256"] for result in results
    }
    cases_consistent = (
        len(runtime_identities) == 1
        and len(catalog_hashes) == 1
        and None not in catalog_hashes
        and len(launcher_layout_hashes) == 1
    )
    passed = (
        all(result["passed"] for result in results)
        and cases_consistent
        and non_admin
        and os.name == "nt"
    )
    payload = {
        "schema_version": 1,
        "kind": "v2_retrieval_launcher_matrix",
        "environment": {
            "os": platform.system().lower(),
            "os_release": platform.release(),
            "python_version": platform.python_version(),
            "non_admin": non_admin,
        },
        "requirements": {
            "windows_required": True,
            "non_admin_required": True,
            "case_labels": list(_REQUIRED_CASE_LABELS),
            "post_successor_state": {
                "mode": "native",
                "write_epoch": "positive",
                "v1_fallback_open": False,
                "degraded": False,
                "write_enabled": True,
            },
            "cross_case_runtime_identity_required": True,
            "cross_case_catalog_hash_required": True,
            "case_specific_install_roots_required": True,
            "cross_case_launcher_layout_hash_required": True,
            "surfaces": [
                "launcher_guard",
                "gui",
                "cli",
                "quick_start",
                "run_app_bat",
                "run_quickstart_bat",
            ],
        },
        "cases_consistent": cases_consistent,
        "cases": results,
        "passed": passed,
    }
    _write_immutable_json(args.output, payload)
    print(
        json.dumps(
            {
                "status": "passed" if passed else "failed",
                "evidence": args.output.name,
                "case_count": len(results),
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


def _parse_case(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not _LABEL.fullmatch(label) or not raw_path:
        raise argparse.ArgumentTypeError("--case must use stable-label=DB_PATH")
    db_input = Path(raw_path)
    db_path = db_input.resolve(strict=False)
    if (
        (db_path.exists() and (not db_path.is_file() or db_path.is_symlink()))
        or not db_path.parent.is_dir()
        or db_path.parent.is_symlink()
    ):
        raise argparse.ArgumentTypeError("launcher case data root is unavailable or unsafe")
    catalog = db_path.parent / "retrieval" / "v2" / "catalog.sqlite3"
    if not catalog.is_file() or catalog.is_symlink():
        raise argparse.ArgumentTypeError("launcher case native catalog is unavailable or unsafe")
    return label, db_path


def _parse_install_root(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not _LABEL.fullmatch(label) or not raw_path:
        raise argparse.ArgumentTypeError("--install-root must use stable-label=PATH")
    root = Path(raw_path).resolve(strict=False)
    if not root.is_dir() or root.is_symlink():
        raise argparse.ArgumentTypeError("launcher install root is unavailable or unsafe")
    required = (
        "apps/cli/app.py",
        "apps/gui/app.py",
        "scripts/quickstart.py",
        "RUN_APP.bat",
        "RUN_QUICKSTART.bat",
        "MIGRATE_V2.bat",
        "tools/recovery/REBUILD_V2.bat",
        "scripts/migrations/v2/migrate_v2_user.py",
        "scripts/migrations/v2/rebuild_v2_successor.py",
        "src/retrieval/launcher_guard.py",
        "src/retrieval/update_lock.py",
    )
    for relative in required:
        candidate = root.joinpath(*relative.split("/"))
        if not candidate.is_file() or candidate.is_symlink():
            raise argparse.ArgumentTypeError(
                f"launcher install root is missing required layout: {relative}"
            )
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file() or python.is_symlink():
        raise argparse.ArgumentTypeError(
            "launcher install root requires its own .venv Python"
        )
    return label, root


def _run_case(
    label: str,
    install_root: Path,
    db_path: Path,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    root = install_root
    python = root / ".venv" / "Scripts" / "python.exe"
    environment = os.environ.copy()
    environment["DB_PATH"] = str(db_path)
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    surfaces = (
        (
            "launcher_guard",
            [str(python), "-m", "src.retrieval.launcher_guard"],
        ),
        (
            "gui",
            [str(python), "apps/gui/app.py", "--runtime-smoke"],
        ),
        (
            "cli",
            [str(python), "-m", "apps.cli.app", "--runtime-smoke"],
        ),
        (
            "quick_start",
            [str(python), "scripts/quickstart.py", "--runtime-smoke"],
        ),
        (
            "run_app_bat",
            ["cmd", "/d", "/c", "RUN_APP.bat", "--runtime-smoke"],
        ),
        (
            "run_quickstart_bat",
            ["cmd", "/d", "/c", "RUN_QUICKSTART.bat", "--runtime-smoke"],
        ),
    )
    catalog = db_path.parent / "retrieval" / "v2" / "catalog.sqlite3"
    launcher_layout_sha256 = _launcher_layout_sha256(root)
    catalog_sha256_before = _sha256_file(catalog)
    outcomes = []
    observed_identities: list[dict[str, Any]] = []
    for surface, command in surfaces:
        started = time.perf_counter_ns()
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=timeout_seconds,
            )
            duration_ns = time.perf_counter_ns() - started
            combined = completed.stdout + completed.stderr
            runtime_identity = _validated_runtime_identity(combined)
            if runtime_identity is not None:
                observed_identities.append(runtime_identity)
            outcomes.append(
                {
                    "surface": surface,
                    "exit_code": completed.returncode,
                    "duration_ns": duration_ns,
                    "output_sha256": hashlib.sha256(combined).hexdigest(),
                    "runtime_identity_sha256": (
                        None
                        if runtime_identity is None
                        else hashlib.sha256(
                            canonical_json(runtime_identity).encode("utf-8")
                        ).hexdigest()
                    ),
                    "passed": completed.returncode == 0
                    and runtime_identity is not None,
                    **(
                        {}
                        if runtime_identity is not None
                        else {"error_code": "launcher_runtime_output_invalid"}
                    ),
                }
            )
        except subprocess.TimeoutExpired as exc:
            combined = (exc.stdout or b"") + (exc.stderr or b"")
            outcomes.append(
                {
                    "surface": surface,
                    "exit_code": None,
                    "duration_ns": time.perf_counter_ns() - started,
                    "output_sha256": hashlib.sha256(combined).hexdigest(),
                    "passed": False,
                    "error_code": "launcher_timeout",
                }
            )
        except OSError as exc:
            outcomes.append(
                {
                    "surface": surface,
                    "exit_code": None,
                    "duration_ns": time.perf_counter_ns() - started,
                    "output_sha256": hashlib.sha256(str(type(exc).__name__).encode()).hexdigest(),
                    "passed": False,
                    "error_code": "launcher_unavailable",
                }
            )
    catalog_sha256_after = _sha256_file(catalog)
    identity_encodings = {
        canonical_json(identity) for identity in observed_identities
    }
    runtime_identity = (
        observed_identities[0]
        if len(observed_identities) == len(surfaces)
        and len(identity_encodings) == 1
        else None
    )
    passed = (
        all(outcome["passed"] for outcome in outcomes)
        and runtime_identity is not None
        and catalog_sha256_before == catalog_sha256_after
    )
    return {
        "label": label,
        "launcher_layout_sha256": launcher_layout_sha256,
        "catalog_sha256": catalog_sha256_after,
        "catalog_unchanged": catalog_sha256_before == catalog_sha256_after,
        "runtime_identity": runtime_identity,
        "passed": passed,
        "surfaces": outcomes,
    }


def _validated_runtime_identity(output: bytes) -> dict[str, Any] | None:
    payloads: list[dict[str, Any]] = []
    for line in output.decode("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "status" in value:
            payloads.append(value)
    if not payloads:
        return None

    identities: list[dict[str, Any]] = []
    for payload in payloads:
        generation = payload.get("publication_generation")
        epoch = payload.get("write_epoch")
        snapshot_id = payload.get("active_snapshot_id")
        if (
            payload.get("status") != "ok"
            or payload.get("mode") != "native"
            or not isinstance(snapshot_id, str)
            or not snapshot_id
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation <= 0
            or not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch <= 0
            or payload.get("v1_fallback_open") is not False
            or payload.get("degraded") is not False
            or payload.get("write_enabled") is not True
        ):
            return None
        identities.append(
            {
                "active_snapshot_id": snapshot_id,
                "publication_generation": generation,
                "write_epoch": epoch,
                "v1_fallback_open": False,
                "degraded": False,
                "write_enabled": True,
            }
        )
    if len({canonical_json(identity) for identity in identities}) != 1:
        return None
    return identities[0]


def _is_admin() -> bool:
    if os.name != "nt":
        return bool(getattr(os, "geteuid", lambda: 1)() == 0)
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"launcher evidence already exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.parent / f".launcher-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def _launcher_layout_sha256(root: Path) -> str:
    files = [
        root / "scripts" / "quickstart.py",
        root / "RUN_APP.bat",
        root / "RUN_QUICKSTART.bat",
        root / "MIGRATE_V2.bat",
        root / "tools" / "recovery" / "REBUILD_V2.bat",
        root / "scripts" / "migrations" / "v2" / "migrate_v2_user.py",
        root / "scripts" / "migrations" / "v2" / "rebuild_v2_successor.py",
        *sorted((root / "apps").rglob("*.py")),
        *sorted((root / "src").rglob("*.py")),
    ]
    digest = hashlib.sha256()
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError("launcher layout contains an unavailable or unsafe file")
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
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


if __name__ == "__main__":
    raise SystemExit(main())
