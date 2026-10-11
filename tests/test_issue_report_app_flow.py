from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from streamlit.testing.v1 import AppTest

from src.core import issue_report_outbox


RAW_EMAIL = "reporter@example.com"
RAW_TOKEN = "bearer abcdefghijk"
RAW_SECRET = "super-secret-value"
RAW_PATH = r"C:\Users\alice\private-report.pdf"


def _write_harness(path: Path, *, outbox_dir: Path, debug_dir: Path) -> None:
    path.write_text(
        f'''
from unittest.mock import patch
from pathlib import Path

from apps.gui import chat_views
from src.core import issue_report_outbox, issue_report_store

THREAD = {{"id": "thread-e2e", "name": "신고 E2E"}}
MESSAGES = [
    {{
        "id": "user-1",
        "role": "user",
        "content": {RAW_PATH!r} + " 내용을 확인해줘",
    }},
    {{
        "id": "assistant-1",
        "role": "assistant",
        "content": "API_KEY=" + {RAW_SECRET!r} + " 가 노출된 잘못된 답변",
        "metadata": {{
            "status": "succeeded",
            "route": "vectordb",
            "rerank_info": [{{
                "rank": 1,
                "file_name": "safe-report.pdf",
                "target_name": "테스트 기업",
                "report_uid": "report-safe",
                "chunk_uid": "chunk-safe",
            }}],
            "monitoring": {{
                "routing": {{"route": "vectordb"}},
                "retrieval": {{"context_count": 1}},
                "state_trace": {{
                    "input": {{
                        "prior_search_scope": {{
                            "route": "vectordb",
                            "search_filters": {{"target_name": "테스트 기업"}},
                            "file_names": ["safe-report.pdf"],
                        }}
                    }}
                }},
            }},
        }},
    }},
]
CONFIG = issue_report_outbox.DeliveryConfig(
    enabled=True,
    ingest_url="https://example.supabase.co/functions/v1/issue-report-ingest",
    publishable_key="sb_publishable_test-only",
    outbox_dir=Path({str(outbox_dir)!r}),
)

with patch.object(
    issue_report_outbox, "load_delivery_config", return_value=CONFIG
), patch.object(
    issue_report_outbox, "start_delivery_worker", return_value=False
), patch.object(
    issue_report_store, "DEBUG_REPORT_DIR", Path({str(debug_dir)!r})
):
    chat_views._render_issue_report_control(
        current_thread=THREAD,
        messages=MESSAGES,
    )
''',
        encoding="utf-8",
    )


def _queued_payload(database_path: Path) -> dict:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT payload_json FROM outbox_events"
        ).fetchall()
    assert len(rows) == 1
    return json.loads(rows[0][0])


def _open_report_form(app: AppTest) -> AppTest:
    app.run(timeout=30)
    assert not app.exception
    next(button for button in app.button if button.label == "신고").click().run(
        timeout=30
    )
    assert not app.exception
    return app


def test_issue_report_submit_queues_redacted_consented_payload(
    tmp_path: Path,
) -> None:
    outbox_dir = tmp_path / "outbox"
    debug_dir = tmp_path / "debug-reports"
    harness = tmp_path / "issue_report_harness.py"
    _write_harness(harness, outbox_dir=outbox_dir, debug_dir=debug_dir)
    app = _open_report_form(AppTest.from_file(str(harness)))

    description = f"연락처 {RAW_EMAIL}; {RAW_TOKEN}"
    next(area for area in app.text_area if area.label == "추가 설명(선택)").input(
        description
    )
    for label in (
        "추가 설명을 원격 신고에 포함",
        "선택한 질문과 응답 내용을 원격 신고에 포함",
        "선택한 응답까지의 질문과 검색 상태를 원격 신고에 포함",
    ):
        next(box for box in app.checkbox if box.label == label).check()
    app.run(timeout=30)

    next(button for button in app.button if button.label == "신고 제출").click().run(
        timeout=30
    )

    assert not app.exception
    assert [message.value for message in app.success] == ["신고가 접수되었습니다."]
    assert not any(item.label == "추가 설명(선택)" for item in app.text_area)
    assert not debug_dir.exists()

    payload = _queued_payload(outbox_dir / "issue-report-outbox.sqlite3")
    report = payload["report"]
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["consent"] == {
        "consent_version": 1,
        "include_comment": True,
        "include_selected_question": True,
        "include_selected_answer": True,
        "include_previous_turns": True,
    }
    assert report["comment"]
    assert report["observed"]["selected_question"]
    assert report["observed"]["selected_answer"]
    assert report["observed"]["turn_trace"]
    for raw_value in (RAW_EMAIL, RAW_TOKEN, RAW_SECRET, RAW_PATH):
        assert raw_value not in serialized


def test_issue_report_default_consent_queues_metadata_only(
    tmp_path: Path,
) -> None:
    outbox_dir = tmp_path / "outbox"
    harness = tmp_path / "issue_report_default_consent_harness.py"
    _write_harness(
        harness,
        outbox_dir=outbox_dir,
        debug_dir=tmp_path / "debug-reports",
    )
    app = _open_report_form(AppTest.from_file(str(harness)))

    next(area for area in app.text_area if area.label == "추가 설명(선택)").input(
        f"{RAW_EMAIL} {RAW_SECRET}"
    )
    app.run(timeout=30)
    next(button for button in app.button if button.label == "신고 제출").click().run(
        timeout=30
    )

    assert not app.exception
    payload = _queued_payload(outbox_dir / "issue-report-outbox.sqlite3")
    report = payload["report"]
    assert report["consent"] == {
        "consent_version": 1,
        "include_comment": False,
        "include_selected_question": False,
        "include_selected_answer": False,
        "include_previous_turns": False,
    }
    assert report["comment"] == ""
    assert report["observed"]["selected_question"] is None
    assert report["observed"]["selected_answer"] is None
    assert report["observed"]["turn_trace"] == []
