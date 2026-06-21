from src.core.monitoring import (
    build_message_monitoring_rows,
    compact_graph_monitoring_metadata,
    summarize_chat_messages,
    summarize_evaluation_dataset,
)


def test_summarize_evaluation_dataset_counts_monitoring_dimensions():
    dataset = {
        "name": "finance_llm_local_eval_dataset",
        "version": 2,
        "generated_from": {"snapshot_date": "2026-06-19"},
        "stability_policy": {"policy": "fixed_baseline_until_change_reason"},
        "cases": [
            {
                "type": "vectordb_retrieval",
                "monitoring_dimensions": ["retrieval", "rerank"],
                "criteria_tags": ["local_reproducibility"],
                "expected_sources": [{"file_name": "a.pdf"}],
            },
            {
                "type": "rdb_aggregate",
                "monitoring_dimensions": ["rdb"],
                "criteria_tags": ["route_coverage"],
            },
        ],
    }

    summary = summarize_evaluation_dataset(dataset)

    assert summary["case_count"] == 2
    assert summary["case_types"] == {"vectordb_retrieval": 1, "rdb_aggregate": 1}
    assert summary["monitoring_dimensions"] == {"retrieval": 1, "rerank": 1, "rdb": 1}
    assert summary["expected_source_count"] == 1
    assert summary["stability_policy"]["policy"] == "fixed_baseline_until_change_reason"


def test_chat_monitoring_summary_and_rows_are_safe_metadata_only():
    messages = [
        {"role": "user", "content": "질문 본문"},
        {
            "role": "assistant",
            "content": "긴 답변 본문",
            "created_at": "2026-06-21T00:00:00",
            "metadata": {
                "status": "succeeded",
                "route": "vectordb",
                "latency_seconds": 1.5,
                "rerank_info": [{"rank": 1}, {"rank": 2}],
                "search_scope": {"search_filters": {"target_name": "NAVER"}},
            },
        },
        {
            "role": "assistant",
            "content": "오류 본문",
            "metadata": {"status": "failed", "error": "boom"},
        },
    ]

    summary = summarize_chat_messages(messages)
    rows = build_message_monitoring_rows(messages)

    assert summary["message_count"] == 3
    assert summary["statuses"] == {"succeeded": 1, "failed": 1}
    assert summary["routes"] == {"vectordb": 1}
    assert summary["avg_rerank_source_count"] == 2.0
    assert rows[0]["search_filters"] == {"target_name": "NAVER"}
    assert "content" not in rows[0]


def test_compact_graph_monitoring_metadata_keeps_route_filters_and_scores():
    metadata = compact_graph_monitoring_metadata(
        final_state={
            "route": "vectordb",
            "rewritten_query": "NAVER 최신 리포트",
            "uses_chat_history": False,
            "followup_scope_intent": False,
            "search_filters": {"target_name": "NAVER"},
            "temporal_context": None,
        },
        latency_seconds=1.23456,
        rerank_info=[
            {"score": 0.2, "rerank_score": 0.8, "final_score": 0.9},
            {"score": 0.4, "rerank_score": 0.6, "final_score": 0.7},
        ],
    )

    assert metadata["route"] == "vectordb"
    assert metadata["latency_seconds"] == 1.235
    assert metadata["search_filters"] == {"target_name": "NAVER"}
    assert metadata["monitoring"]["retrieval"]["source_count"] == 2
    assert metadata["monitoring"]["retrieval"]["score_summary"]["rerank_score"]["avg"] == 0.7
