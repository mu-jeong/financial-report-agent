from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from scripts.migrations.v2 import run_release_pytest


def _repository(root: Path) -> Path:
    for relative, content in {
        "src/package.py": "VALUE = 1\n",
        "scripts/helper.py": "VALUE = 2\n",
        "apps/cli.py": "VALUE = 3\n",
        "tests/test_alpha.py": "def test_alpha(): pass\n",
        "tests/fixture.json": '{"value": 1}\n',
        "scripts/quickstart.py": "VALUE = 4\n",
        "RUN_APP.bat": "@echo off\n",
        "RUN_QUICKSTART.bat": "@echo off\n",
        "MIGRATE_V2.bat": "@echo off\n",
        "tools/recovery/REBUILD_V2.bat": "@echo off\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _completed(stdout: str = "", *, returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout.encode("utf-8"),
        stderr=b"",
    )


def _junit_xml(*, tests: int = 2, cases: int = 2) -> str:
    testcase_xml = "".join(
        f'<testcase classname="tests.test_alpha" name="test_{index}" time="0.001" />'
        for index in range(cases)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests">'
        f'<testsuite name="pytest" tests="{tests}" failures="0" errors="0" skipped="0" time="0.002">'
        f"{testcase_xml}</testsuite></testsuites>"
    )


def _fake_pytest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    junit_tests: int = 2,
    junit_cases: int = 2,
    mutate_after_collection: Path | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        assert kwargs["cwd"] == run_release_pytest.REPOSITORY_ROOT
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
        assert "PYTEST_ADDOPTS" not in environment
        if "--collect-only" in argv:
            if mutate_after_collection is not None:
                mutate_after_collection.write_text("changed\n", encoding="utf-8")
            return _completed(
                "tests/test_alpha.py::test_1\n"
                "tests/test_alpha.py::test_2\n\n"
                "2 tests collected in 0.01s\n"
            )
        junit_argument = next(value for value in argv if value.startswith("--junitxml="))
        Path(junit_argument.partition("=")[2]).write_text(
            _junit_xml(tests=junit_tests, cases=junit_cases),
            encoding="utf-8",
        )
        return _completed("2 passed in 0.01s\n")

    monkeypatch.setattr(run_release_pytest.subprocess, "run", run)
    return calls


def test_runner_seals_full_suite_collection_junit_and_layouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _repository(tmp_path / "repo")
    monkeypatch.setattr(run_release_pytest, "REPOSITORY_ROOT", root)
    calls = _fake_pytest(monkeypatch)
    junit = tmp_path / "evidence" / "pytest.xml"
    attestation = tmp_path / "evidence" / "pytest-attestation.json"

    result = run_release_pytest.run_release_tests(
        junit_output=junit,
        attestation_output=attestation,
    )

    assert result == attestation
    assert len(calls) == 2
    assert calls[0][1:] == [
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
    ]
    assert calls[1][1:10] == [
        "-m",
        "pytest",
        "-c",
        os.devnull,
        "--rootdir=.",
        "--noconftest",
        "-q",
        "--disable-warnings",
        "tests",
    ]
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["kind"] == "v2_release_pytest_attestation"
    assert payload["status"] == "passed"
    assert payload["protocol"] == {
        "collection_exit_code": 0,
        "execution_exit_code": 0,
        "selection_args_allowed": False,
        "test_target": "tests",
    }
    assert payload["collection"]["count"] == 2
    assert payload["collection"]["nodeids"] == [
        "tests/test_alpha.py::test_1",
        "tests/test_alpha.py::test_2",
    ]
    assert payload["junit"]["testcase_count"] == 2
    assert payload["junit"]["sha256"] == hashlib.sha256(junit.read_bytes()).hexdigest()
    assert payload["layouts"]["test_file_count"] == 2
    assert payload["layouts"]["source_file_count"] == 8
    assert payload["commands"]["working_directory"] == str(root)
    assert payload["interpreter"]["executable_sha256"] == run_release_pytest._sha256_file(
        Path(payload["interpreter"]["executable"])
    )
    assert junit.stat().st_mode & stat.S_IWRITE == 0
    assert attestation.stat().st_mode & stat.S_IWRITE == 0

    os.chmod(junit, stat.S_IREAD | stat.S_IWRITE)
    os.chmod(attestation, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize(
    ("junit_tests", "junit_cases", "message"),
    [
        (1, 2, "summary"),
        (1, 1, "collection"),
    ],
)
def test_runner_fails_closed_when_junit_does_not_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    junit_tests: int,
    junit_cases: int,
    message: str,
):
    root = _repository(tmp_path / "repo")
    monkeypatch.setattr(run_release_pytest, "REPOSITORY_ROOT", root)
    _fake_pytest(
        monkeypatch,
        junit_tests=junit_tests,
        junit_cases=junit_cases,
    )
    junit = tmp_path / "pytest.xml"
    attestation = tmp_path / "pytest-attestation.json"

    with pytest.raises(run_release_pytest.ReleasePytestError, match=message):
        run_release_pytest.run_release_tests(
            junit_output=junit,
            attestation_output=attestation,
        )

    assert not junit.exists()
    assert not attestation.exists()


def test_runner_fails_closed_on_layout_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = _repository(tmp_path / "repo")
    monkeypatch.setattr(run_release_pytest, "REPOSITORY_ROOT", root)
    _fake_pytest(
        monkeypatch,
        mutate_after_collection=root / "tests" / "test_alpha.py",
    )

    with pytest.raises(run_release_pytest.ReleasePytestError, match="layout changed"):
        run_release_pytest.run_release_tests(
            junit_output=tmp_path / "pytest.xml",
            attestation_output=tmp_path / "pytest-attestation.json",
        )

    assert not (tmp_path / "pytest.xml").exists()
    assert not (tmp_path / "pytest-attestation.json").exists()


def test_cli_refuses_partial_test_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    called = False

    def fail_if_called(**_kwargs: object) -> Path:
        nonlocal called
        called = True
        raise AssertionError("runner must reject selection before execution")

    monkeypatch.setattr(run_release_pytest, "run_release_tests", fail_if_called)

    with pytest.raises(SystemExit) as raised:
        run_release_pytest.main(
            [
                "--junit-output",
                str(tmp_path / "pytest.xml"),
                "--attestation-output",
                str(tmp_path / "pytest-attestation.json"),
                "tests/test_alpha.py",
            ]
        )

    assert raised.value.code == 2
    assert called is False
