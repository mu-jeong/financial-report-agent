"""Run and seal the complete release pytest suite without selection overrides."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import stat
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COLLECTION_TIMEOUT_SECONDS = 10 * 60
EXECUTION_TIMEOUT_SECONDS = 60 * 60
LAYOUT_ALGORITHM = "sha256-path-nul-content-sha256-newline-v1"

_COLLECTION_ARGUMENTS = (
    "-m",
    "pytest",
    "-c",
    os.devnull,
    "--rootdir=.",
    "--noconftest",
    "--collect-only",
    "-q",
    "--disable-warnings",
    "tests",
)
_EXECUTION_ARGUMENTS = (
    "-m",
    "pytest",
    "-c",
    os.devnull,
    "--rootdir=.",
    "--noconftest",
    "-q",
    "--disable-warnings",
    "tests",
)
_IGNORED_LAYOUT_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
_ROOT_SOURCE_FILES = (
    "quickstart.py",
    "RUN_APP.bat",
    "RUN_QUICKSTART.bat",
    "MIGRATE_V2.bat",
    "REBUILD_V2.bat",
)
_COLLECTED_SUMMARY = re.compile(r"(?m)^(\d+) tests? collected(?: in .*)?$")


class ReleasePytestError(RuntimeError):
    """Raised when complete-suite pytest evidence cannot be proved."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the fixed full tests/ suite and seal JUnit provenance"
    )
    parser.add_argument("--junit-output", type=Path, required=True)
    parser.add_argument("--attestation-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        attestation = run_release_tests(
            junit_output=args.junit_output,
            attestation_output=args.attestation_output,
        )
    except ReleasePytestError as exc:
        parser.exit(1, f"release pytest failed: {exc}\n")
    print(
        json.dumps(
            {
                "status": "passed",
                "junit": args.junit_output.name,
                "attestation": attestation.name,
            },
            sort_keys=True,
        )
    )
    return 0


def run_release_tests(
    *,
    junit_output: str | Path,
    attestation_output: str | Path,
) -> Path:
    """Run exactly ``tests/`` and atomically publish read-only evidence."""

    root = _validated_repository_root(REPOSITORY_ROOT)
    junit_target = _new_output_path(junit_output, root, "JUnit")
    attestation_target = _new_output_path(
        attestation_output,
        root,
        "pytest attestation",
    )
    if junit_target == attestation_target:
        raise ReleasePytestError("JUnit and attestation outputs must be distinct")

    token = uuid.uuid4().hex[:16]
    junit_temporary = junit_target.parent / f".{junit_target.name}.{token}.tmp"
    attestation_temporary = (
        attestation_target.parent / f".{attestation_target.name}.{token}.tmp"
    )
    if (
        junit_temporary.exists()
        or junit_temporary.is_symlink()
        or attestation_temporary.exists()
        or attestation_temporary.is_symlink()
    ):
        raise ReleasePytestError("temporary evidence path collision")

    executable = _safe_existing_file(Path(sys.executable), "Python interpreter")
    environment, recorded_environment = _pytest_environment()
    collection_argv = [str(executable), *_COLLECTION_ARGUMENTS]
    execution_argv = [
        str(executable),
        *_EXECUTION_ARGUMENTS,
        f"--junitxml={junit_temporary}",
    ]
    initial_layouts = _capture_release_layouts(root)

    try:
        collection = _invoke_pytest(
            collection_argv,
            root=root,
            environment=environment,
            timeout_seconds=COLLECTION_TIMEOUT_SECONDS,
            phase="collection",
        )
        nodeids = _parse_collected_nodeids(collection.stdout)
        _require_unchanged_layouts(root, initial_layouts)

        _invoke_pytest(
            execution_argv,
            root=root,
            environment=environment,
            timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
            phase="execution",
        )
        _require_unchanged_layouts(root, initial_layouts)
        junit_path = _safe_temporary_file(junit_temporary, "pytest JUnit")
        junit_summary = _validate_junit(junit_path, collected_count=len(nodeids))
        _require_unchanged_layouts(root, initial_layouts)
        _flush_file(junit_path)
        junit_sha256 = _sha256_file(junit_path)

        nodeids_sha256 = _sha256_json(nodeids)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "kind": "v2_release_pytest_attestation",
            "status": "passed",
            "protocol": {
                "collection_exit_code": collection.returncode,
                "execution_exit_code": 0,
                "selection_args_allowed": False,
                "test_target": "tests",
            },
            "commands": {
                "working_directory": str(root),
                "collection_argv": collection_argv,
                "execution_argv": execution_argv,
                "environment": recorded_environment,
            },
            "interpreter": {
                "executable": str(executable),
                "executable_sha256": _sha256_file(executable),
                "implementation": platform.python_implementation(),
                "python_version": platform.python_version(),
                "pytest_version": importlib.metadata.version("pytest"),
            },
            "layouts": initial_layouts,
            "collection": {
                "count": len(nodeids),
                "nodeids": nodeids,
                "nodeids_sha256": nodeids_sha256,
            },
            "junit": {
                **junit_summary,
                "output_name": junit_target.name,
                "sha256": junit_sha256,
            },
        }
        _write_temporary_json(attestation_temporary, payload)
        _require_unchanged_layouts(root, initial_layouts)
        _publish_read_only_pair(
            (
                (junit_temporary, junit_target),
                (attestation_temporary, attestation_target),
            )
        )
        return attestation_target
    except ReleasePytestError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReleasePytestError("release pytest evidence could not be sealed") from exc
    finally:
        _remove_owned_temporary(junit_temporary)
        _remove_owned_temporary(attestation_temporary)


def _validated_repository_root(value: Path) -> Path:
    if _path_has_reparse_component(value):
        raise ReleasePytestError("repository root contains a symlink or junction")
    try:
        root = value.resolve(strict=True)
    except OSError as exc:
        raise ReleasePytestError("repository root is unavailable") from exc
    for relative in ("tests", "src", "scripts", "apps"):
        path = root / relative
        if not path.is_dir() or path.is_symlink():
            raise ReleasePytestError(f"required repository directory is unavailable: {relative}")
    return root


def _new_output_path(value: str | Path, root: Path, label: str) -> Path:
    candidate = Path(value)
    if candidate.exists() or candidate.is_symlink():
        raise ReleasePytestError(f"{label} output path must be new")
    protected = [root / name for name in ("tests", "src", "scripts", "apps")]
    protected.extend(root / name for name in _ROOT_SOURCE_FILES)
    unresolved_target = candidate.resolve(strict=False)
    if any(
        unresolved_target == path or _is_relative_to(unresolved_target, path)
        for path in protected
    ):
        raise ReleasePytestError(
            f"{label} output must be outside source and test layouts"
        )
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if _path_has_reparse_component(candidate.parent):
            raise ReleasePytestError(f"{label} output parent is unsafe")
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise ReleasePytestError(f"{label} output parent is unavailable") from exc
    target = parent / candidate.name
    if not candidate.name or target.exists() or target.is_symlink():
        raise ReleasePytestError(f"{label} output path must be new")
    if any(target == path or _is_relative_to(target, path) for path in protected):
        raise ReleasePytestError(f"{label} output must be outside source and test layouts")
    return target


def _pytest_environment() -> tuple[dict[str, str], dict[str, str]]:
    environment = os.environ.copy()
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
    ):
        environment.pop(name, None)
    enforced = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    environment.update(enforced)
    return environment, enforced


def _invoke_pytest(
    argv: list[str],
    *,
    root: Path,
    environment: Mapping[str, str],
    timeout_seconds: int,
    phase: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleasePytestError(f"pytest {phase} failed to complete") from exc
    if completed.returncode != 0:
        output_sha256 = hashlib.sha256(
            (completed.stdout or b"") + b"\0" + (completed.stderr or b"")
        ).hexdigest()
        raise ReleasePytestError(
            f"pytest {phase} failed with exit code {completed.returncode} "
            f"(output SHA-256 {output_sha256})"
        )
    return completed


def _parse_collected_nodeids(output: bytes) -> list[str]:
    try:
        text = output.decode("utf-8", errors="strict").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise ReleasePytestError("pytest collection output is not UTF-8") from exc
    matches = _COLLECTED_SUMMARY.findall(text)
    if len(matches) != 1:
        raise ReleasePytestError("pytest collection summary is missing or ambiguous")
    nodeids = [
        line.strip()
        for line in text.splitlines()
        if line.startswith("tests/") and "::" in line
    ]
    if (
        not nodeids
        or len(nodeids) != int(matches[0])
        or len(nodeids) != len(set(nodeids))
        or any("\x00" in nodeid for nodeid in nodeids)
    ):
        raise ReleasePytestError("pytest collection nodeids are incomplete or invalid")
    return nodeids


def _capture_release_layouts(root: Path) -> dict[str, Any]:
    test_files = _tree_files(root, root / "tests", include_all=True)
    source_files: list[Path] = []
    for relative in ("src", "scripts", "apps"):
        source_files.extend(
            _tree_files(root, root / relative, include_all=False)
        )
    source_files.extend(root / name for name in _ROOT_SOURCE_FILES)
    return {
        "algorithm": LAYOUT_ALGORITHM,
        "test_file_count": len(test_files),
        "test_layout_sha256": _layout_sha256(root, test_files),
        "source_file_count": len(source_files),
        "source_layout_sha256": _layout_sha256(root, source_files),
    }


def _tree_files(root: Path, tree: Path, *, include_all: bool) -> list[Path]:
    if _path_has_reparse_component(tree):
        raise ReleasePytestError("release layout contains a symlink or junction")
    files: list[Path] = []
    for path in sorted(tree.rglob("*")):
        relative_parts = path.relative_to(tree).parts
        if any(part in _IGNORED_LAYOUT_DIRECTORIES for part in relative_parts):
            continue
        metadata = path.lstat()
        if _is_reparse(metadata) or path.is_symlink():
            raise ReleasePytestError("release layout contains a symlink or junction")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleasePytestError("release layout contains an unsupported object")
        if path.suffix == ".pyc" or (not include_all and path.suffix != ".py"):
            continue
        files.append(path)
    if not files:
        raise ReleasePytestError(
            f"release layout is empty: {tree.relative_to(root).as_posix()}"
        )
    return files


def _layout_sha256(root: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    seen: set[str] = set()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        safe = _safe_existing_file(path, "release layout file")
        relative = safe.relative_to(root).as_posix()
        if relative in seen:
            raise ReleasePytestError("release layout contains a duplicate path")
        seen.add(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_stable_file(safe)))
        digest.update(b"\n")
    return digest.hexdigest()


def _require_unchanged_layouts(root: Path, expected: Mapping[str, Any]) -> None:
    if _capture_release_layouts(root) != dict(expected):
        raise ReleasePytestError("source or test layout changed during release pytest")


def _validate_junit(path: Path, *, collected_count: int) -> dict[str, int]:
    try:
        encoded = path.read_bytes()
        if b"<!DOCTYPE" in encoded.upper() or b"<!ENTITY" in encoded.upper():
            raise ReleasePytestError("pytest JUnit contains an unsafe declaration")
        root = ET.fromstring(encoded)
    except (OSError, ET.ParseError) as exc:
        raise ReleasePytestError("pytest JUnit is unreadable") from exc
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root)
        if not suites or any(child.tag != "testsuite" for child in suites):
            raise ReleasePytestError("pytest JUnit suite structure is invalid")
    else:
        raise ReleasePytestError("pytest JUnit root is invalid")

    total = failures = errors = skipped = 0
    identities: set[tuple[str, str]] = set()
    for suite in suites:
        cases = list(suite.findall("testcase"))
        if any(child.tag == "testsuite" for child in suite):
            raise ReleasePytestError("pytest JUnit nested suites are unsupported")
        suite_failures = suite_errors = suite_skipped = 0
        for case in cases:
            classname = case.get("classname")
            name = case.get("name")
            identity = (classname or "", name or "")
            if not all(identity) or identity in identities:
                raise ReleasePytestError("pytest JUnit testcase identities are invalid")
            identities.add(identity)
            outcomes = [
                child.tag
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ]
            if len(outcomes) > 1:
                raise ReleasePytestError("pytest JUnit testcase has multiple outcomes")
            suite_failures += outcomes.count("failure")
            suite_errors += outcomes.count("error")
            suite_skipped += outcomes.count("skipped")
            _require_nonnegative_finite(case.get("time", "0"), "testcase time")
        supplied = {
            "tests": _nonnegative_int(suite.get("tests"), "suite tests"),
            "failures": _nonnegative_int(
                suite.get("failures"), "suite failures"
            ),
            "errors": _nonnegative_int(suite.get("errors"), "suite errors"),
            "skipped": _nonnegative_int(suite.get("skipped"), "suite skipped"),
        }
        calculated = {
            "tests": len(cases),
            "failures": suite_failures,
            "errors": suite_errors,
            "skipped": suite_skipped,
        }
        if supplied != calculated:
            raise ReleasePytestError("pytest JUnit suite summary does not match testcases")
        _require_nonnegative_finite(suite.get("time", "0"), "suite time")
        total += len(cases)
        failures += suite_failures
        errors += suite_errors
        skipped += suite_skipped

    calculated_total = {
        "tests": total,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }
    if root.tag == "testsuites":
        root_counts = {name: root.get(name) for name in calculated_total}
        if any(value is not None for value in root_counts.values()):
            if any(value is None for value in root_counts.values()):
                raise ReleasePytestError("pytest JUnit root summary is incomplete")
            supplied_total = {
                name: _nonnegative_int(value, f"root {name}")
                for name, value in root_counts.items()
            }
            if supplied_total != calculated_total:
                raise ReleasePytestError(
                    "pytest JUnit root summary does not match testcases"
                )
        _require_nonnegative_finite(root.get("time", "0"), "root time")
    if total != collected_count:
        raise ReleasePytestError(
            "pytest JUnit testcase count does not match full-suite collection"
        )
    if failures or errors:
        raise ReleasePytestError("pytest JUnit contains a failed or errored testcase")
    return {
        "suite_count": len(suites),
        "testcase_count": total,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
    }


def _nonnegative_int(value: str | None, label: str) -> int:
    try:
        parsed = int(value or "")
    except ValueError as exc:
        raise ReleasePytestError(f"pytest JUnit {label} is invalid") from exc
    if parsed < 0 or str(parsed) != value:
        raise ReleasePytestError(f"pytest JUnit {label} is invalid")
    return parsed


def _require_nonnegative_finite(value: str, label: str) -> None:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ReleasePytestError(f"pytest JUnit {label} is invalid") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ReleasePytestError(f"pytest JUnit {label} is invalid")


def _safe_existing_file(path: Path, label: str) -> Path:
    if _path_has_reparse_component(path) or path.is_symlink():
        raise ReleasePytestError(f"{label} is unavailable or unsafe")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReleasePytestError(f"{label} is unavailable or unsafe") from exc
    if not resolved.is_file():
        raise ReleasePytestError(f"{label} is unavailable or unsafe")
    return resolved


def _safe_temporary_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ReleasePytestError(f"{label} was not produced safely")
    resolved = path.resolve(strict=True)
    if resolved != path.resolve(strict=False):
        raise ReleasePytestError(f"{label} was not produced safely")
    return resolved


def _sha256_stable_file(path: Path) -> str:
    before = path.stat()
    digest = _sha256_file(path)
    after = path.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise ReleasePytestError("release layout file changed while hashing")
    return digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _write_temporary_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _flush_file(path: Path) -> None:
    # Windows' CRT rejects ``_commit`` for a read-only descriptor. The file is
    # still private staging here, so open it update-capable without changing it.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _publish_read_only_pair(pairs: Sequence[tuple[Path, Path]]) -> None:
    published: list[Path] = []
    try:
        for temporary, target in pairs:
            if target.exists() or target.is_symlink():
                raise ReleasePytestError("release pytest output path changed before publication")
            os.link(temporary, target)
            published.append(target)
        for temporary, _target in pairs:
            temporary.unlink()
        for target in published:
            os.chmod(target, stat.S_IREAD)
    except (OSError, ReleasePytestError) as exc:
        for target in reversed(published):
            try:
                os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
                target.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(exc, ReleasePytestError):
            raise
        raise ReleasePytestError("release pytest outputs could not be published") from exc


def _remove_owned_temporary(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        if not path.is_symlink():
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        path.unlink()
    except OSError:
        pass


def _path_has_reparse_component(path: Path) -> bool:
    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        try:
            metadata = component.lstat()
        except OSError:
            continue
        if component.is_symlink() or _is_reparse(metadata):
            return True
    return False


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_point)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
