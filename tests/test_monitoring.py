from pathlib import Path

from src.core.monitoring import (
    build_issue_report_rows,
    build_monitoring_page_labels,
    build_evaluation_failure_actions,
    build_message_monitoring_rows,
    build_monitoring_tab_labels,
    compare_evaluation_runs,
    compact_graph_monitoring_metadata,
    evaluate_dataset_case_result,
    promote_issue_report_to_eval_candidate,
    run_evaluation_dataset,
    select_evaluation_cases,
    summarize_all_chat_threads,
    summarize_chat_messages,
    summarize_data_integrity,
    summarize_evaluation_dataset,
    summarize_issue_reports,
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
            "scope_decision": {"reason": "matched_prior_section_alias"},
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
    assert metadata["scope_decision"] == {"reason": "matched_prior_section_alias"}
    assert metadata["monitoring"]["retrieval"]["source_count"] == 2
    assert metadata["monitoring"]["retrieval"]["score_summary"]["rerank_score"]["avg"] == 0.7


def test_evaluate_dataset_case_result_scores_route_filter_source_citation_and_latency():
    case = {
        "id": "case-1",
        "question": "NAVER 최신 리포트 요약",
        "expected_route": "vectordb",
        "expected_filters": {"target_name": "NAVER"},
        "expected_sources": [{"file_name": "naver.pdf"}],
    }
    final_state = {
        "route": "vectordb",
        "search_filters": {"target_name": "NAVER", "report_type": "company"},
        "generation": "핵심 내용입니다 [1]",
        "rerank_info": [{"rank": 1, "file_name": "naver.pdf"}, {"rank": 2, "file_name": "other.pdf"}],
        "no_vector_results": False,
    }

    result = evaluate_dataset_case_result(case, final_state, latency_seconds=1.2, latency_threshold_seconds=3.0)

    assert result["status"] == "pass"
    assert result["route_pass"] is True
    assert result["filter_pass"] is True
    assert result["source_hit"] is True
    assert result["hit_at_k"] == 1
    assert result["citation_valid"] is True
    assert result["latency_pass"] is True


def test_evaluate_dataset_case_result_fails_bad_route_missing_source_and_bad_citation():
    case = {
        "id": "case-2",
        "question": "NAVER 최신 리포트 요약",
        "expected_route": "vectordb",
        "expected_filters": {"target_name": "NAVER"},
        "expected_sources": [{"file_name": "naver.pdf"}],
    }
    final_state = {
        "route": "rdb",
        "search_filters": {"target_name": "카카오"},
        "generation": "근거가 없습니다 [3]",
        "rerank_info": [{"rank": 1, "file_name": "other.pdf"}],
        "no_vector_results": True,
    }

    result = evaluate_dataset_case_result(case, final_state, latency_seconds=9.0, latency_threshold_seconds=3.0)

    assert result["status"] == "fail"
    assert result["route_pass"] is False
    assert result["filter_pass"] is False
    assert result["source_hit"] is False
    assert result["hit_at_k"] is None
    assert result["citation_valid"] is False
    assert result["latency_pass"] is False
    assert result["no_result"] is True


def test_run_evaluation_dataset_saves_run_and_summary(tmp_path):
    dataset = {
        "name": "local_eval",
        "version": 2,
        "cases": [
            {
                "id": "case-1",
                "question": "NAVER 요약",
                "expected_route": "vectordb",
                "expected_filters": {"target_name": "NAVER"},
                "expected_sources": [{"file_name": "naver.pdf"}],
            }
        ],
    }

    def fake_invoke(payload, config=None):
        assert payload["question"] == "NAVER 요약"
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "답변 [1]",
            "rerank_info": [{"rank": 1, "file_name": "naver.pdf"}],
        }

    run = run_evaluation_dataset(dataset, fake_invoke, output_dir=tmp_path)

    assert run["summary"]["case_count"] == 1
    assert run["summary"]["passed"] == 1
    assert run["summary"]["source_hit_rate"] == 1.0
    assert Path(run["json_path"]).exists()


def test_compare_evaluation_runs_reports_metric_deltas():
    previous = {"summary": {"passed": 1, "failed": 2, "avg_latency_seconds": 3.0, "source_hit_rate": 0.5}}
    current = {"summary": {"passed": 2, "failed": 1, "avg_latency_seconds": 2.0, "source_hit_rate": 0.75}}

    comparison = compare_evaluation_runs(current, previous)

    assert comparison["passed_delta"] == 1
    assert comparison["failed_delta"] == -1
    assert comparison["avg_latency_delta"] == -1.0
    assert comparison["source_hit_rate_delta"] == 0.25


def test_summarize_all_chat_threads_and_issue_reports_for_global_monitoring():
    thread_messages = [
        {
            "thread": {"id": "thread-a", "name": "NAVER"},
            "messages": [
                {"role": "assistant", "created_at": "2026-06-21", "metadata": {"status": "succeeded", "route": "vectordb", "latency_seconds": 2.0, "no_vector_results": True}},
                {"role": "assistant", "created_at": "2026-06-22", "metadata": {"status": "failed", "error": "boom"}},
            ],
        }
    ]
    reports = [
        {"id": "r1", "thread_id": "thread-a", "category": "답변 품질", "created_at": "2026-06-21", "file_path": "debug/r1.txt", "content": "Finance LLM 문제 신고\nDescription:\n오답"}
    ]

    chat_summary = summarize_all_chat_threads(thread_messages)
    report_summary = summarize_issue_reports(reports)
    rows = build_issue_report_rows(reports, thread_names={"thread-a": "NAVER"})

    assert chat_summary["thread_count"] == 1
    assert chat_summary["failure_rate"] == 0.5
    assert chat_summary["no_result_rate"] == 1.0
    assert report_summary["categories"] == {"답변 품질": 1}
    assert rows[0]["thread_name"] == "NAVER"
    assert "Description" in rows[0]["preview"]


def test_summarize_data_integrity_flags_missing_indexes_and_pending_embeddings():
    summary = summarize_data_integrity(
        {
            "db": {"total_reports": 10, "embedded_reports": 7, "pending_reports": 3, "parent_chunks": 0},
            "vector_db": {"has_faiss_index": False, "file_count": 0},
            "downloaded_pdfs": 6,
            "search_coverage_ratio": 0.7,
        }
    )

    assert summary["checks"]["faiss_index"]["status"] == "fail"
    assert summary["checks"]["embedding_backlog"]["status"] == "warning"
    assert summary["checks"]["pdf_vs_db"]["status"] == "warning"


def test_monitoring_tab_labels_separate_global_and_chat_monitoring():
    assert build_monitoring_tab_labels() == [
        "데이터/설정",
        "실험 실행",
        "고정 테스트셋",
        "Parsing engines",
        "전체 Monitoring",
        "Chat Monitoring",
        "Issue reports",
    ]

def test_promote_issue_report_to_eval_candidate_saves_regression_candidate(tmp_path):
    candidate = promote_issue_report_to_eval_candidate(
        {
            "id": "r1",
            "thread_id": "thread-a",
            "category": "답변 품질",
            "created_at": "2026-06-21",
            "file_path": "debug/r1.txt",
            "content": "Finance LLM 문제 신고\nDescription:\n답변이 이상합니다\nContext:\n...",
        },
        output_dir=tmp_path,
    )

    assert candidate["source_report_id"] == "r1"
    assert candidate["status"] == "candidate"
    assert candidate["recommended_next_step"] == "convert_to_evaluation_dataset_case"
    assert Path(candidate["json_path"]).exists()

def test_monitoring_page_labels_make_global_monitoring_directly_accessible():
    assert build_monitoring_page_labels() == [
        "Chat",
        "전체 Monitoring",
    ]

def test_select_evaluation_cases_uses_selected_ids_not_count():
    dataset = {
        "cases": [
            {"id": "case-a", "question": "A"},
            {"id": "case-b", "question": "B"},
            {"id": "case-c", "question": "C"},
        ]
    }

    selected = select_evaluation_cases(dataset, ["case-c", "case-a"])

    assert [case["id"] for case in selected] == ["case-c", "case-a"]


def test_run_evaluation_dataset_runs_selected_case_ids_only(tmp_path):
    dataset = {
        "name": "local_eval",
        "version": 2,
        "cases": [
            {"id": "case-a", "question": "A", "expected_route": "vectordb"},
            {"id": "case-b", "question": "B", "expected_route": "vectordb"},
        ],
    }
    seen_questions = []

    def fake_invoke(payload, config=None):
        seen_questions.append(payload["question"])
        return {"route": "vectordb", "generation": "", "rerank_info": []}

    run = run_evaluation_dataset(dataset, fake_invoke, output_dir=tmp_path, selected_case_ids=["case-b"])

    assert seen_questions == ["B"]
    assert run["selected_case_ids"] == ["case-b"]
    assert run["summary"]["case_count"] == 1


def test_build_evaluation_failure_actions_explains_next_steps_by_failure_type():
    failed_result = {
        "case_id": "case-1",
        "status": "fail",
        "route_pass": False,
        "filter_pass": False,
        "source_hit": False,
        "citation_valid": False,
        "latency_pass": False,
        "no_result": True,
    }

    actions = build_evaluation_failure_actions([failed_result])

    assert actions[0]["case_id"] == "case-1"
    assert "router/query classification" in actions[0]["recommended_actions"]
    assert "metadata filter extraction" in actions[0]["recommended_actions"]
    assert "retrieval index, chunking, or rerank" in actions[0]["recommended_actions"]
    assert "citation generation/removal" in actions[0]["recommended_actions"]
    assert "latency budget" in actions[0]["recommended_actions"]
    assert "retry with broader filters or update data" in actions[0]["recommended_actions"]
