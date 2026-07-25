from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrations.v2 import run_v2_launcher_matrix


def _launcher_install(root: Path) -> Path:
    payloads = {
        "apps/cli/app.py": b"# cli\n",
        "apps/gui/app.py": b"# gui\n",
        "scripts/quickstart.py": b"# quickstart\n",
        "RUN_APP.bat": b"@echo off\r\n",
        "RUN_QUICKSTART.bat": b"@echo off\r\n",
        "MIGRATE_V2.bat": b"@echo off\r\n",
        "tools/recovery/REBUILD_V2.bat": b"@echo off\r\n",
        "scripts/migrations/v2/migrate_v2_user.py": b"# migrate\n",
        "scripts/migrations/v2/rebuild_v2_successor.py": b"# rebuild\n",
        "src/retrieval/launcher_guard.py": b"# guard\n",
        "src/retrieval/update_lock.py": b"# update lock\n",
        ".venv/Scripts/python.exe": b"synthetic-python",
    }
    for relative, payload in payloads.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return root


def test_launcher_matrix_evidence_is_redacted_and_covers_all_surfaces(
    tmp_path,
    monkeypatch,
):
    case_paths = {
        "source-default": tmp_path / "source-default" / "reports.db",
        "packaged-default": tmp_path / "packaged-default" / "reports.db",
        "custom-local": tmp_path / "한글 root with spaces" / "reports.db",
    }
    for db_path in case_paths.values():
        db_path.parent.mkdir()
        db_path.write_bytes(b"legacy-catalog-fixture")
        catalog = db_path.parent / "retrieval" / "v2" / "catalog.sqlite3"
        catalog.parent.mkdir(parents=True)
        catalog.write_bytes(b"native-catalog-fixture")
    source_install = _launcher_install(tmp_path / "source-install")
    packaged_install = _launcher_install(tmp_path / "packaged-install")
    install_roots = {
        "source-default": source_install,
        "packaged-default": packaged_install,
        "custom-local": source_install,
    }
    output = tmp_path / "launcher-evidence.json"
    commands = []
    run_kwargs = []

    def fake_run(command, **kwargs):
        commands.append(command)
        run_kwargs.append(kwargs)
        payload = {
            "status": "ok",
            "mode": "native",
            "active_snapshot_id": "snapshot-successor",
            "publication_generation": 2,
            "write_epoch": 1,
            "v1_fallback_open": False,
            "degraded": False,
            "write_enabled": True,
        }
        return SimpleNamespace(
            returncode=0,
            stdout=(json.dumps(payload) + "\n").encode(),
            stderr=b"",
        )

    monkeypatch.setattr(run_v2_launcher_matrix.subprocess, "run", fake_run)
    monkeypatch.setattr(run_v2_launcher_matrix, "_is_admin", lambda: False)

    result = run_v2_launcher_matrix.main(
        [
            "--case",
            f"source-default={case_paths['source-default']}",
            "--case",
            f"packaged-default={case_paths['packaged-default']}",
            "--case",
            f"custom-local={case_paths['custom-local']}",
            "--install-root",
            f"source-default={install_roots['source-default']}",
            "--install-root",
            f"packaged-default={install_roots['packaged-default']}",
            "--install-root",
            f"custom-local={install_roots['custom-local']}",
            "--output",
            str(output),
            "--require-non-admin",
        ]
    )

    encoded = output.read_text(encoding="utf-8")
    evidence = json.loads(encoded)
    assert result == 0
    assert evidence["passed"] is True
    assert len(commands) == 18
    assert all(kwargs["stdin"] is subprocess.DEVNULL for kwargs in run_kwargs)
    assert [case["label"] for case in evidence["cases"]] == list(
        run_v2_launcher_matrix._REQUIRED_CASE_LABELS
    )
    assert all(len(case["surfaces"]) == 6 for case in evidence["cases"])
    assert any(
        command[-2:] == ["apps/gui/app.py", "--runtime-smoke"]
        for command in commands
    )
    assert evidence["requirements"]["non_admin_required"] is True
    assert evidence["requirements"]["windows_required"] is True
    assert evidence["cases_consistent"] is True
    assert all(case["launcher_layout_sha256"] for case in evidence["cases"])
    assert all(str(db_path) not in encoded for db_path in case_paths.values())
    assert "한글 root with spaces" not in encoded


def test_launcher_matrix_rejects_incomplete_case_contract(tmp_path):
    db_path = tmp_path / "custom 한글 root" / "reports.db"
    db_path.parent.mkdir()
    db_path.touch()
    catalog = db_path.parent / "retrieval" / "v2" / "catalog.sqlite3"
    catalog.parent.mkdir(parents=True)
    catalog.touch()

    with pytest.raises(SystemExit, match="2"):
        run_v2_launcher_matrix.main(
            [
                "--case",
                f"custom-local={db_path}",
                "--output",
                str(tmp_path / "invalid-evidence.json"),
            ]
        )


def test_launcher_matrix_cannot_pass_as_an_administrator(tmp_path, monkeypatch):
    case_paths = {
        "source-default": tmp_path / "source-default" / "reports.db",
        "packaged-default": tmp_path / "packaged-default" / "reports.db",
        "custom-local": tmp_path / "custom 한글 root" / "reports.db",
    }
    for db_path in case_paths.values():
        db_path.parent.mkdir()
        db_path.write_bytes(b"legacy-catalog-fixture")
        catalog = db_path.parent / "retrieval" / "v2" / "catalog.sqlite3"
        catalog.parent.mkdir(parents=True)
        catalog.write_bytes(b"native-catalog-fixture")
    source_install = _launcher_install(tmp_path / "source-install")
    packaged_install = _launcher_install(tmp_path / "packaged-install")
    output = tmp_path / "administrator-evidence.json"

    monkeypatch.setattr(
        run_v2_launcher_matrix.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                json.dumps(
                    {
                        "status": "ok",
                        "mode": "native",
                        "active_snapshot_id": "snapshot-successor",
                        "publication_generation": 2,
                        "write_epoch": 1,
                        "v1_fallback_open": False,
                        "degraded": False,
                        "write_enabled": True,
                    }
                )
                + "\n"
            ).encode(),
            stderr=b"",
        ),
    )
    monkeypatch.setattr(run_v2_launcher_matrix, "_is_admin", lambda: True)

    result = run_v2_launcher_matrix.main(
        [
            "--case",
            f"source-default={case_paths['source-default']}",
            "--case",
            f"packaged-default={case_paths['packaged-default']}",
            "--case",
            f"custom-local={case_paths['custom-local']}",
            "--install-root",
            f"source-default={source_install}",
            "--install-root",
            f"packaged-default={packaged_install}",
            "--install-root",
            f"custom-local={source_install}",
            "--output",
            str(output),
        ]
    )

    assert result == 1
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is False


@pytest.mark.parametrize(
    "overrides",
    (
        {"mode": "legacy_v1", "active_snapshot_id": None},
        {
            "write_epoch": 0,
            "v1_fallback_open": True,
            "write_enabled": False,
        },
        {"degraded": True, "write_enabled": False},
    ),
)
def test_launcher_matrix_rejects_non_successor_runtime_payloads(overrides):
    payload = {
        "status": "ok",
        "mode": "native",
        "active_snapshot_id": "snapshot-successor",
        "publication_generation": 2,
        "write_epoch": 1,
        "v1_fallback_open": False,
        "degraded": False,
        "write_enabled": True,
        **overrides,
    }

    assert (
        run_v2_launcher_matrix._validated_runtime_identity(
            (json.dumps(payload) + "\n").encode()
        )
        is None
    )


def test_launcher_matrix_accepts_missing_legacy_anchor_with_native_catalog(tmp_path):
    catalog = tmp_path / "retrieval" / "v2" / "catalog.sqlite3"
    catalog.parent.mkdir(parents=True)
    catalog.touch()
    anchor = tmp_path / "reports.db"

    assert run_v2_launcher_matrix._parse_case(
        f"source-default={anchor}"
    ) == ("source-default", anchor.resolve())


def test_launcher_matrix_rejects_a_missing_native_catalog(tmp_path):
    with pytest.raises(
        run_v2_launcher_matrix.argparse.ArgumentTypeError,
        match="native catalog is unavailable or unsafe",
    ):
        run_v2_launcher_matrix._parse_case(
            f"source-default={tmp_path / 'missing-reports.db'}"
        )
