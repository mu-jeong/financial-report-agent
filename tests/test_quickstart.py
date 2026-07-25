from scripts import quickstart
from types import SimpleNamespace


def test_ensure_env_writes_quickstart_defaults_without_prompt(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    env_example_path = tmp_path / ".env.example"
    env_path.write_text("OPENROUTER_API_KEY=sk-or-v1-test\n", encoding="utf-8")
    env_example_path.write_text("OPENROUTER_API_KEY=your_openrouter_api_key_here\n", encoding="utf-8")

    monkeypatch.setattr(quickstart, "ENV_PATH", env_path)
    monkeypatch.setattr(quickstart, "ENV_EXAMPLE_PATH", env_example_path)
    monkeypatch.setattr(
        quickstart,
        "quickstart_env_updates",
        lambda: {
            "CRAWLER_MODE": "LATEST",
            "CRAWLER_TARGET_DATE": "2026-06-03",
            "CRAWLER_LOOKBACK_DAYS": "7",
            "CRAWLER_TARGET_COUNT": "0",
            "CRAWLER_MAX_LOOKBACK_DAYS": "7",
        },
    )
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("prompted")))

    quickstart.ensure_env()

    content = env_path.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert "OPENROUTER_API_KEY=sk-or-v1-test" in content
    assert "CRAWLER_TARGET_DATE=2026-06-03" in content
    assert "기준일: 2026-06-03" in output


def test_print_runtime_status_uses_status_module_not_deprecated_cli(monkeypatch):
    calls = []

    def fake_run_command(command, *, description, progress=None):
        calls.append((command, description, progress))

    monkeypatch.setattr(quickstart, "run_command", fake_run_command)
    monkeypatch.setattr(quickstart, "VENV_PYTHON", "python")

    quickstart.print_runtime_status()

    command, description, progress = calls[0]
    assert description == "데이터 상태 확인"
    assert progress is None
    assert "apps/cli/app.py" not in command
    assert "-c" in command
    assert "format_status_text" in command[-1]
    assert "format_readiness_text" not in command[-1]


def test_prepare_data_runs_write_guard_before_crawler(monkeypatch):
    calls = []

    def fake_run_command(command, *, description, progress=None):
        calls.append((command, description, progress))

    monkeypatch.setattr(quickstart, "run_command", fake_run_command)
    monkeypatch.setattr(quickstart, "VENV_PYTHON", "python")
    monkeypatch.setattr(quickstart, "print_runtime_status", lambda progress=None: None)

    quickstart.prepare_data()

    assert calls[0][0] == ["python", "-m", "src.retrieval.launcher_guard", "--write"]
    assert calls[1][0] == ["python", "-m", "src.core.report_crawler"]
    assert calls[2][0] == [
        "python",
        "-m",
        "src.core.embed_pipeline",
        "--all",
        "--continue-on-extraction-error",
    ]


def test_progress_tracker_prints_step_progress(capsys):
    progress = quickstart.ProgressTracker(total=3, width=6)

    progress.advance("Python 확인")
    progress.advance("환경 설정")

    output = capsys.readouterr().out
    assert "[진행]" in output
    assert "1/3" in output
    assert "2/3" in output
    assert "[####--]" in output


def test_runtime_smoke_runs_only_installed_launcher_guard(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    calls = []

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(quickstart, "VENV_PYTHON", python)
    monkeypatch.setattr(quickstart.subprocess, "run", fake_run)

    assert quickstart.main(["--runtime-smoke"]) == 0
    assert calls == [
        ([str(python), "-m", "src.retrieval.launcher_guard"], quickstart.ROOT, False)
    ]
