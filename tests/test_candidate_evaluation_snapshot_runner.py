from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import run_candidate_evaluation_snapshot as runner


def test_parser_requires_candidate_and_run_kind():
    with pytest.raises(SystemExit):
        runner.parse_args(["--run-kind", "baseline"])
    with pytest.raises(SystemExit):
        runner.parse_args(["--candidate", "candidate.json"])

    args = runner.parse_args(
        ["--candidate", "candidate.json", "--run-kind", "verification"]
    )

    assert args.dataset == "tests/fixtures/evaluation_dataset.json"
    assert args.snapshot_root == "tests/fixtures/eval_snapshot"
    assert args.output_dir == "debug/candidate_evaluation_runs"
    assert args.run_kind == "verification"
    assert args.latency_threshold_seconds == 30.0


def test_build_snapshot_environment_isolated_from_base():
    base = {"UNCHANGED": "yes"}
    validation = {
        "db_path": Path("snapshot") / "reports.db",
        "faiss_dir": Path("snapshot") / "vector_db",
    }

    env = runner.build_snapshot_environment(validation, base_env=base)

    assert base == {"UNCHANGED": "yes"}
    assert env["UNCHANGED"] == "yes"
    assert env["DB_PATH"] == str(validation["db_path"])
    assert env["FAISS_DIR"] == str(validation["faiss_dir"])
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"


def test_provenance_has_exact_keys_and_allowlisted_canonical_fingerprint():
    first = {
        "name": "fixed",
        "version": 3,
        "dataset_name": "dataset",
        "dataset_version": 2,
        "snapshot_date": "2026-06-19",
        "database": {
            "path": "reports.db",
            "table": "reports",
            "source_row_count": 10,
            "embedded_row_count": 9,
            "min_report_date": "2026-01-01",
            "max_report_date": "2026-06-19",
            "ignored": "not identity",
        },
        "vector_db": {
            "path": "vector_db",
            "required_files": ["index.faiss", "index.pkl"],
            "file_sizes": {"index.pkl": 2, "index.faiss": 1},
        },
        "config": {"ignored": True},
    }
    second = {
        **first,
        "database": dict(reversed(list(first["database"].items()))),
        "untrusted_extra": "ignored",
    }

    provenance = runner.build_candidate_provenance(first)

    assert set(provenance) == {
        "backend_mode",
        "snapshot_id",
        "snapshot_available",
        "data_revision",
        "config_fingerprint",
    }
    assert provenance["backend_mode"] == "fixed_snapshot"
    assert provenance["snapshot_id"] == "fixed:3"
    assert provenance["snapshot_available"] is True
    assert provenance["data_revision"] == "2026-06-19"
    assert (
        provenance["config_fingerprint"]
        == runner.manifest_config_fingerprint(second)
    )
    expected_json = json.dumps(
        runner.build_manifest_identity(first),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert provenance["config_fingerprint"] == hashlib.sha256(
        expected_json
    ).hexdigest()


def test_missing_snapshot_returns_json_error_without_importing_graph(
    tmp_path, monkeypatch, capsys
):
    candidate = {
        "integrity_status": "valid",
        "triage_status": "ready",
    }
    monkeypatch.setattr(
        runner, "load_regression_candidate", lambda _path: candidate
    )
    monkeypatch.setattr(
        runner, "validate_candidate_for_run", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        runner,
        "load_evaluation_dataset",
        lambda _path: {"name": "dataset"},
    )
    captured: dict = {}

    def fake_blocked_run(candidate_value, invoke_fn, **kwargs):
        captured["candidate"] = candidate_value
        captured.update(kwargs)
        return {
            "json_path": str(tmp_path / "blocked.json"),
            "run_id": "blocked-run",
            "run_status": "blocked",
            "blocked_reason": "snapshot_unavailable",
        }

    monkeypatch.setattr(
        runner,
        "run_candidate_evaluation",
        fake_blocked_run,
    )

    missing_root = tmp_path / "missing_snapshot"
    exit_code = runner.main(
        [
            "--candidate",
            str(tmp_path / "candidate.json"),
            "--snapshot-root",
            str(missing_root),
            "--run-kind",
            "baseline",
        ]
    )

    output_lines = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(output_lines[-1])
    assert exit_code == 2
    assert payload["status"] == "error"
    assert payload["stage"] == "load_manifest"
    assert payload["error_type"] == "FileNotFoundError"
    assert payload["run_status"] == "blocked"
    assert payload["blocked_reason"] == "snapshot_unavailable"
    assert captured["provenance"]["snapshot_available"] is False
    assert captured["provenance"]["backend_mode"] == "fixed_snapshot"
    assert "Traceback" not in "\n".join(output_lines)


def test_blocked_snapshot_attempt_is_persisted_without_invoking_graph(
    tmp_path,
):
    candidate = {
        "id": "candidate_blocked",
        "triage_status": "ready",
        "contract_revision": 1,
        "expected_approved_at": "2026-07-26T00:00:00+00:00",
        "expected_approved_by": "local_operator",
        "verification_type": "graph_contract",
        "active_checks": ["route_pass"],
        "observed": {
            "reproduction_input": {"question": "합성 질문"},
            "actual": {},
        },
        "expected": {
            "route": "vectordb",
            "filters": {},
            "sources": [],
            "state": {},
            "manual_assertions": [],
        },
    }
    args = runner.parse_args(
        [
            "--candidate",
            "candidate.json",
            "--run-kind",
            "baseline",
            "--output-dir",
            str(tmp_path / "runs"),
        ]
    )

    run = runner.record_blocked_snapshot_attempt(
        candidate,
        args=args,
        stage="validate_snapshot",
    )

    assert run["run_status"] == "blocked"
    assert run["blocked_reason"] == "snapshot_unavailable"
    assert run["provenance"]["snapshot_available"] is False
    assert Path(run["json_path"]).exists()


def test_success_path_invokes_graph_with_fixed_snapshot_environment(
    tmp_path, monkeypatch, capsys
):
    candidate = {
        "integrity_status": "valid",
        "triage_status": "ready",
        "id": "candidate-ok",
        "contract_revision": 1,
        "candidate_hash": "candidate-ok-hash",
        "expected_approved_at": "2026-07-26T12:00:00+00:00",
        "verification_type": "graph_contract",
        "active_checks": ["route_pass"],
        "observed": {"reproduction_input": {"question": "Q?"}},
        "expected": {
            "route": "vectordb",
            "filters": {},
            "sources": [],
            "state": {},
            "manual_assertions": [],
        },
    }
    manifest = {
        "name": "fixed",
        "version": 3,
        "dataset_name": "dataset",
        "dataset_version": 1,
        "snapshot_date": "2026-06-19",
        "database": {
            "path": "reports.db",
            "table": "reports",
            "source_row_count": 10,
            "embedded_row_count": 9,
            "min_report_date": "2026-01-01",
            "max_report_date": "2026-06-19",
        },
        "vector_db": {
            "path": "vector_db",
            "required_files": ["index.faiss", "index.pkl"],
            "file_sizes": {"index.pkl": 2, "index.faiss": 1},
        },
    }

    monkeypatch.setattr(
        runner, "load_regression_candidate", lambda _path: candidate
    )
    monkeypatch.setattr(runner, "load_evaluation_dataset", lambda _path: {
        "name": "dataset",
        "version": 1,
        "generated_from": {
            "snapshot_date": "2026-06-19",
            "source_row_count": 10,
            "embedded_row_count": 9,
            "min_report_date": "2026-01-01",
            "max_report_date": "2026-06-19",
        },
    })

    def fake_validate(_dataset, _manifest, _root):
        return {
            "status": "pass",
            "db_path": str(tmp_path / "snapshot" / "reports.db"),
            "faiss_dir": str(tmp_path / "snapshot" / "vector_db"),
            "checks": [],
        }

    monkeypatch.setattr(runner, "load_evaluation_snapshot_manifest", lambda _path: manifest)
    monkeypatch.setattr(runner, "validate_evaluation_snapshot", fake_validate)

    captured = {}
    module_name = "src.graphs.main_graph"
    import types

    fake_graph_module = types.ModuleType(module_name)

    def fake_invoke(payload, config=None):
        captured["payload"] = payload
        captured["config"] = config
        return {"route": "ok", "filters": {}, "sources": [], "state": {}}

    fake_graph_module.graph_app = types.SimpleNamespace(invoke=fake_invoke)
    monkeypatch.setitem(runner.sys.modules, module_name, fake_graph_module)

    def fake_run_candidate_evaluation(
        _candidate, _invoke_fn, **kwargs
    ):
        captured["run_kwargs"] = kwargs
        return {
            "json_path": str(tmp_path / "run.json"),
            "run_id": "run-success",
            "run_status": "completed",
            "blocked_reason": kwargs.get("provenance", {}).get("snapshot_available")
            is False,
        }

    monkeypatch.setattr(runner, "run_candidate_evaluation", fake_run_candidate_evaluation)
    monkeypatch.setenv("DB_PATH", "preexisting-db")
    monkeypatch.setenv("FAISS_DIR", "preexisting-faiss")

    exit_code = runner.main(
        [
            "--candidate",
            "candidate.json",
            "--run-kind",
            "baseline",
            "--output-dir",
            str(tmp_path / "runs"),
            "--snapshot-root",
            str(tmp_path / "snapshot_root"),
        ]
    )

    output_lines = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(output_lines[-1])
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["run_id"] == "run-success"
    assert captured["run_kwargs"]["provenance"]["backend_mode"] == "fixed_snapshot"
    assert captured["run_kwargs"]["provenance"]["snapshot_available"] is True
    assert captured["run_kwargs"]["run_kind"] == "baseline"
    assert captured["run_kwargs"]["provenance"]["snapshot_id"] == "fixed:3"
    assert runner.os.environ.get("DB_PATH") == str(tmp_path / "snapshot" / "reports.db")
    assert runner.os.environ.get("FAISS_DIR") == str(tmp_path / "snapshot" / "vector_db")
    assert captured["run_kwargs"]["provenance"]["data_revision"] == "2026-06-19"
