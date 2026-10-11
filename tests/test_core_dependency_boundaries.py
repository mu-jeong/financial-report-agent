"""Regression coverage for dependency-light core helpers."""

from __future__ import annotations

import json
import subprocess
import sys


def _isolated_import(module_name: str) -> dict[str, bool]:
    script = (
        "import json, sys\n"
        f"import {module_name}\n"
        "print(json.dumps({"
        "'config': 'src.configs.config' in sys.modules, "
        "'monitoring': 'src.core.monitoring' in sys.modules"
        "}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_report_metadata_import_is_dependency_light():
    assert _isolated_import("src.core.report_metadata") == {
        "config": False,
        "monitoring": False,
    }


def test_text_redaction_import_is_dependency_light():
    assert _isolated_import("src.core.text_redaction") == {
        "config": False,
        "monitoring": False,
    }


def test_outbox_import_does_not_load_monitoring_or_config():
    assert _isolated_import("src.core.issue_report_outbox") == {
        "config": False,
        "monitoring": False,
    }


def test_legacy_filename_parser_import_remains_compatible():
    from src.core.db_manager import parse_filename as legacy_parse_filename
    from src.core.report_metadata import parse_filename

    assert legacy_parse_filename is parse_filename
    assert legacy_parse_filename(
        "company_2026-02-05_Samsung_Broker_HBM_AI outlook.pdf"
    ) == {
        "report_type": "company",
        "report_date": "2026-02-05",
        "target_name": "Samsung",
        "broker": "Broker",
        "title": "HBM_AI outlook",
    }


def test_legacy_redaction_import_remains_compatible():
    from src.core.feedback_handoff import redact_handoff_text as legacy_redact_text
    from src.core.text_redaction import redact_handoff_text

    assert legacy_redact_text is redact_handoff_text
    assert legacy_redact_text("api_key=supersecret") == "[REDACTED:credential]"
