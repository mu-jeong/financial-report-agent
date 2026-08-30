from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def _write_harness(path: Path) -> None:
    path.write_text(
        """
import apps.gui.monitoring_views as views
from unittest.mock import patch

MESSAGES = [
    {"id": 1, "role": "user", "content": "삼성전자 실적을 설명해줘"},
    {
        "id": 2,
        "role": "assistant",
        "content": "삼성전자 실적 답변 [1]",
        "created_at": "2026-08-30T10:00:00",
        "metadata": {
            "status": "succeeded",
            "question": "삼성전자 실적을 설명해줘",
            "route": "vectordb",
            "latency_seconds": 3.2,
            "retrieval_runtime": {
                "mode": "native",
                "active_snapshot_id": "snapshot-v2",
                "publication_generation": 3,
                "write_epoch": 2,
                "degraded": False,
            },
            "search_scope": {
                "search_filters": {"target_name": "삼성전자"},
                "file_names": ["samsung.pdf"],
                "scope_source": "current_turn",
            },
            "selected_sources": [
                {
                    "rank": 1,
                    "file_name": "samsung.pdf",
                    "target_name": "삼성전자",
                    "report_uid": "report-1",
                    "chunk_uid": "chunk-1",
                    "report_date": "2026-08-29",
                    "title": "삼성전자 실적",
                    "broker": "테스트증권",
                }
            ],
            "monitoring": {
                "graph_schema_version": 1,
                "graph_manifest": {
                    "graph_id": "finance_chat",
                    "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "nodes": [
                        {"id": "__start__", "label": "시작", "kind": "boundary", "order": 0},
                        {"id": "query_rewrite", "label": "질문 재작성", "kind": "task", "order": 1},
                        {"id": "__end__", "label": "종료", "kind": "boundary", "order": 2},
                    ],
                    "edges": [
                        {"source": "__start__", "target": "query_rewrite", "conditional": False},
                        {"source": "query_rewrite", "target": "__end__", "conditional": False},
                    ],
                },
                "node_runs": [
                    {
                        "run_id": "run-query-rewrite",
                        "node_id": "query_rewrite",
                        "sequence": 1,
                        "invocation_index": 1,
                        "started_offset_seconds": 0.0,
                        "ended_offset_seconds": 0.12,
                        "status": "completed",
                        "duration_seconds": 0.12,
                        "result_keys": ["rewritten_query"],
                    }
                ],
                "query_rewrite": {
                    "rewritten_query": "삼성전자 최근 실적 리포트",
                    "uses_chat_history": False,
                },
                "routing": {"route_hint": "vectordb"},
                "retrieval": {
                    "native_total_ns": 700000000,
                    "candidate_count_after_filter": 4,
                    "context_count": 1,
                    "selected_file_names": ["samsung.pdf"],
                },
                "generation": {
                    "status": "measured",
                    "call_count": 1,
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "request_ns": 1100000000,
                    "provider_name": "fixture-provider",
                    "model_name": "fixture-model",
                },
                "state_snapshot": {
                    "route": "vectordb",
                    "available_keys": [
                        "rewritten_query",
                        "search_filters",
                        "route",
                    ],
                },
                "state_trace": {
                    "input": {"question": "삼성전자 실적을 설명해줘"},
                    "after_search_scope": {"file_count": 1},
                },
            },
        },
    },
]

def segmented_control(label, options, default=None, key=None, **kwargs):
    return views.st.session_state.get(key, default) if key is not None else default


with patch.object(views.conversation_store, "list_messages", return_value=MESSAGES), patch.object(
    views.st,
    "segmented_control",
    new=segmented_control,
):
    views.render_chat_monitoring_page("thread-1", {"name": "테스트 대화"})
""",
        encoding="utf-8",
    )


def test_chat_monitoring_graph_node_click_updates_the_right_panel(tmp_path: Path) -> None:
    harness = tmp_path / "chat_monitoring_harness.py"
    _write_harness(harness)
    app = AppTest.from_file(str(harness))

    app.run(timeout=20)

    assert not app.exception
    assert [item.value for item in app.header] == ["개별 Chat Monitoring"]
    assert any(item.value == "전체 지표" for item in app.subheader)
    node_button = next(
        button for button in app.button if "질문 재작성" in button.label
    )

    node_button.click().run(timeout=20)

    assert not app.exception
    assert any(item.value == "질문 재작성" for item in app.subheader)
    assert not any(item.value == "전체 지표" for item in app.subheader)
    assert app.session_state["_chat_monitoring_selected_node_thread-1_2"] == (
        "query_rewrite"
    )
