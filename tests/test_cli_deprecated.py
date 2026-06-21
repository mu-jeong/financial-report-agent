import sys

from apps.cli import app as cli_app


def test_status_command_prints_deprecation_notice(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["app.py", "--status"])
    monkeypatch.setattr(cli_app, "format_status_text", lambda: "STATUS")

    cli_app.main()

    output = capsys.readouterr().out
    assert "[DEPRECATED]" in output
    assert "신규 기능 개발은 중단" in output
    assert "STATUS" in output
