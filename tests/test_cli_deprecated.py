import sys
from types import SimpleNamespace

from apps.cli import app as cli_app


def test_status_command_prints_deprecation_notice(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["app.py", "--status"])
    monkeypatch.setattr(cli_app, "format_status_text", lambda: "STATUS")

    cli_app.main()

    output = capsys.readouterr().out
    assert "[DEPRECATED]" in output
    assert "신규 기능 개발은 중단" in output
    assert "STATUS" in output


def test_runtime_smoke_reconciles_before_interactive_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["app.py", "--runtime-smoke"])
    monkeypatch.setattr(
        cli_app,
        "reconcile_and_inspect_runtime",
        lambda _path, *, allow_live_writer_read, prefer_fast_read: SimpleNamespace(
            mode="native",
            active_snapshot_id="snapshot-successor",
            publication_generation=3,
            write_epoch=2,
            degraded=False,
            write_enabled=True,
            initialization_state="ready",
        ),
    )
    monkeypatch.setattr(
        cli_app,
        "run_cli",
        lambda: (_ for _ in ()).throw(AssertionError("interactive CLI started")),
    )

    cli_app.main()

    output = capsys.readouterr().out
    assert '"surface": "cli"' in output
    assert '"active_snapshot_id": "snapshot-successor"' in output
    assert '"write_epoch": 2' in output
    assert '"initialization_state": "ready"' in output
