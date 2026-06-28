from pathlib import Path

from src.core.monitoring import (
    build_chat_trace_debug_hints,
    build_chat_trace_issue_context,
    build_eval_case_draft_from_issue_report,
    build_regression_candidate_dataset,
    build_regression_candidate_rows,
    list_regression_candidates,
    build_message_trace_detail,
    build_message_trace_summary,
    build_issue_report_rows,
    build_monitoring_page_labels,
    build_response_diff,
    run_multiturn_evaluation_dataset,
    load_multiturn_evaluation_dataset,
    evaluate_multiturn_turn_result,
    build_reusable_search_scope,
    build_evaluation_failure_actions,
    filter_evaluation_runs_by_mode,
    build_message_monitoring_rows,
    build_global_monitoring_section_labels,
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
    validate_evaluation_snapshot,
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


def test_validate_evaluation_snapshot_passes_for_matching_manifest_and_files(tmp_path):
    snapshot_root = tmp_path / "eval_snapshot"
    vector_dir = snapshot_root / "vector_db"
    vector_dir.mkdir(parents=True)
    (snapshot_root / "reports.db").write_bytes(b"sqlite placeholder")
    (vector_dir / "index.faiss").write_bytes(b"faiss")
    (vector_dir / "index.pkl").write_bytes(b"pkl")
    dataset = {
        "name": "finance_llm_local_eval_dataset",
        "version": 2,
        "generated_from": {
            "snapshot_date": "2026-06-19",
            "source_row_count": 3861,
            "embedded_row_count": 3847,
            "min_report_date": "2026-02-05",
            "max_report_date": "2026-06-19",
        },
    }
    manifest = {
        "dataset_name": "finance_llm_local_eval_dataset",
        "dataset_version": 2,
        "snapshot_date": "2026-06-19",
        "database": {
            "path": "reports.db",
            "source_row_count": 3861,
            "embedded_row_count": 3847,
            "min_report_date": "2026-02-05",
            "max_report_date": "2026-06-19",
        },
        "vector_db": {"path": "vector_db", "required_files": ["index.faiss", "index.pkl"]},
    }

    validation = validate_evaluation_snapshot(dataset, manifest, snapshot_root)

    assert validation["status"] == "pass"
    assert validation["db_path"] == str(snapshot_root / "reports.db")
    assert validation["faiss_dir"] == str(vector_dir)


def test_validate_evaluation_snapshot_fails_for_missing_files_and_mismatched_dataset(tmp_path):
    dataset = {
        "name": "finance_llm_local_eval_dataset",
        "version": 2,
        "generated_from": {"snapshot_date": "2026-06-19"},
    }
    manifest = {
        "dataset_name": "other_dataset",
        "dataset_version": 3,
        "snapshot_date": "2026-06-20",
        "database": {"path": "reports.db"},
        "vector_db": {"path": "vector_db", "required_files": ["index.faiss"]},
    }

    validation = validate_evaluation_snapshot(dataset, manifest, tmp_path / "missing_snapshot")

    assert validation["status"] == "fail"
    failed_checks = {check["name"] for check in validation["checks"] if check["status"] == "fail"}
    assert "dataset_name" in failed_checks
    assert "dataset_version" in failed_checks
    assert "snapshot_date" in failed_checks
    assert "snapshot_db_exists" in failed_checks
    assert "vector_file:index.faiss" in failed_checks


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
    assert rows[0]["user_question_preview"] == "질문 본문"
    assert rows[0]["assistant_preview"] == "긴 답변 본문"
    assert "content" not in rows[0]



def test_message_monitoring_rows_pair_each_assistant_with_previous_user_question():
    messages = [
        {"id": 1, "role": "user", "content": "지난주 발간된 리포트 모두 정리해줘"},
        {
            "id": 2,
            "role": "assistant",
            "content": "지난주 리포트 요약",
            "created_at": "2026-06-22T10:28:00",
            "metadata": {
                "status": "succeeded",
                "route": "vectordb",
                "latency_seconds": 7.9,
                "rerank_info": [{"file_name": "a.pdf"}, {"file_name": "b.pdf"}],
                "search_filters": {"report_date_start": "2026-06-15"},
                "scope_source": None,
            },
        },
        {"id": 3, "role": "user", "content": "개별 종목에 대해 좀 더 자세히 작성해줘"},
        {
            "id": 4,
            "role": "assistant",
            "content": "개별 종목 상세",
            "created_at": "2026-06-22T10:31:00",
            "metadata": {
                "status": "succeeded",
                "route": "vectordb",
                "latency_seconds": 3.2,
                "rerank_info": [{"file_name": "company.pdf"}],
                "search_filters": {"report_type": "company"},
                "scope_source": "prior_search_scope",
                "scope_decision": {"reason": "matched_prior_section_alias"},
                "no_vector_results": False,
            },
        },
    ]

    rows = build_message_monitoring_rows(messages)

    assert rows[1]["message_id"] == 4
    assert rows[1]["user_question_preview"] == "개별 종목에 대해 좀 더 자세히 작성해줘"
    assert rows[1]["assistant_preview"] == "개별 종목 상세"
    assert rows[1]["scope_decision_reason"] == "matched_prior_section_alias"
    assert rows[1]["selected_file_names"] == ["company.pdf"]
    assert rows[1]["label"].startswith("2026-06-22T10:31:00 · vectordb")


def test_message_trace_detail_splits_response_metadata_into_debug_sections():
    message = {
        "id": 10,
        "role": "assistant",
        "content": "답변 본문 [1]",
        "metadata": {
            "question": "개별 종목 자세히",
            "route": "vectordb",
            "search_filters": {"report_type": "company"},
            "temporal_context": {"report_date_start": "2026-06-15"},
            "selection_context": None,
            "scope_source": "prior_search_scope",
            "scope_decision": {"reason": "matched_prior_section_alias"},
            "search_scope": {
                "route": "vectordb",
                "search_filters": {"report_type": "company"},
                "file_names": ["company.pdf"],
            },
            "rerank_info": [{"rank": 1, "file_name": "company.pdf", "report_type": "company"}],
            "monitoring": {
                "query_rewrite": {
                    "rewritten_query": "개별 종목 자세히",
                    "uses_chat_history": False,
                    "followup_scope_intent": True,
                },
                "retrieval": {
                    "candidate_count_after_filter": 8,
                    "document_coverage_applied": True,
                    "document_coverage_reason": "section_followup_scope",
                    "selected_file_names": ["company.pdf"],
                },
                "state_trace": {
                    "input": {
                        "question": "개별 종목 자세히",
                        "prior_search_scope": {
                            "route": "vectordb",
                            "search_filters": {"report_type": "company"},
                            "file_count": 92,
                            "file_names": ["company.pdf", "industry.pdf"],
                        },
                    }
                },
            },
        },
    }

    detail = build_message_trace_detail(message, user_question="개별 종목 자세히")

    assert detail["query_rewrite"]["original_question"] == "개별 종목 자세히"
    assert detail["scope"]["search_filters"] == {"report_type": "company"}
    assert detail["routing"]["route"] == "vectordb"
    assert detail["state_transitions"]["input"]["prior_search_scope_file_count"] == 92
    assert detail["state_transitions"]["after_search_scope"]["search_scope_file_count"] == 1
    assert detail["state_transitions"]["suspect_transitions"]["prior_scope_files_dropped"] is True
    assert detail["retrieval"]["document_coverage_reason"] == "section_followup_scope"
    assert detail["sources"][0]["report_type"] == "company"
    assert detail["answer"]["citation_ranks_used"] == [1]
    assert detail["answer"]["citation_valid"] is True


def test_message_trace_summary_flattens_common_debug_fields():
    detail = {
        "query_rewrite": {
            "original_question": "반도체 섹터 기업 자세히",
            "rewritten_query": "반도체 섹터 기업 상세 내용",
            "followup_scope_intent": True,
            "scope_source": "industry_company_lookup",
            "scope_decision": {"reason": "industry_company_universe_intersection", "industry_term": "반도체"},
        },
        "scope": {
            "search_filters": {"report_type": "company", "file_names": ["a.pdf", "b.pdf"]},
        },
        "routing": {"route": "vectordb"},
        "retrieval": {"candidate_count_after_filter": 3},
        "answer": {"source_count": 2, "citation_valid": True},
    }

    summary = build_message_trace_summary(
        detail,
        diff={"route_changed": False},
        hints=["source가 한 문서에 편중되었습니다."],
    )

    assert summary["original_question"] == "반도체 섹터 기업 자세히"
    assert summary["route"] == "vectordb"
    assert summary["scope_source"] == "industry_company_lookup"
    assert summary["scope_reason"] == "industry_company_universe_intersection"
    assert summary["industry_term"] == "반도체"
    assert summary["source_count"] == 2
    assert "prior_scope_file_count" in summary
    assert "search_scope_file_count" in summary
    assert summary["search_filters"] == {"report_type": "company", "file_names": ["a.pdf", "b.pdf"]}
    assert summary["debug_hint_count"] == 1
    assert summary["diff_available"] is True


def test_response_diff_reports_filter_source_and_retrieval_changes():
    previous = {
        "content": "이전 답변",
        "metadata": {
            "route": "vectordb",
            "search_filters": {"report_date_start": "2026-06-15", "report_date_end": "2026-06-21"},
            "search_scope": {"file_names": ["a.pdf", "b.pdf"]},
            "rerank_info": [{"file_name": "a.pdf"}, {"file_name": "b.pdf"}],
            "monitoring": {"retrieval": {"candidate_count_after_filter": 33}},
        },
    }
    current = {
        "content": "현재 답변",
        "metadata": {
            "route": "vectordb",
            "search_filters": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-21",
                "report_type": "company",
            },
            "scope_decision": {"reason": "matched_prior_section_alias"},
            "search_scope": {"file_names": ["b.pdf"]},
            "rerank_info": [{"file_name": "b.pdf"}, {"file_name": "c.pdf"}],
            "monitoring": {
                "state_trace": {
                    "input": {"prior_search_scope": {"file_names": ["a.pdf", "b.pdf"]}}
                },
                "retrieval": {"candidate_count_after_filter": 8},
            },
        },
    }

    diff = build_response_diff(current, previous)

    assert diff["search_filters"]["kept"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
    }
    assert diff["search_filters"]["added"] == {"report_type": "company"}
    assert diff["sources"]["added"] == ["c.pdf"]
    assert diff["sources"]["removed"] == ["a.pdf"]
    assert diff["state"]["prior_to_current_file_count"] == {
        "input_prior_search_scope": 2,
        "current_search_scope": 1,
    }
    assert diff["state"]["search_scope_file_count_delta_vs_previous"] == -1
    assert diff["retrieval"]["candidate_count_after_filter_delta"] == -25


def test_chat_trace_debug_hints_flag_common_rag_failures():
    current = {
        "metadata": {
            "followup_scope_intent": True,
            "scope_source": None,
            "route": "rdb",
            "search_filters": {},
            "rerank_info": [{"file_name": "same.pdf"}, {"file_name": "same.pdf"}],
            "monitoring": {
                "retrieval": {
                    "candidate_count_after_filter": 0,
                    "document_coverage_applied": False,
                }
            },
        }
    }
    previous = {
        "metadata": {
            "search_filters": {"report_date_start": "2026-06-15", "report_date_end": "2026-06-21"}
        }
    }

    hints = build_chat_trace_debug_hints(
        current,
        previous,
        user_question="리포트들 각각 주요 내용 요약해줘",
    )

    assert any("prior_search_scope" in hint for hint in hints)
    assert any("날짜 필터" in hint for hint in hints)
    assert any("candidate_count_after_filter=0" in hint for hint in hints)
    assert any("route=rdb" in hint for hint in hints)
    assert any("document_coverage_applied=False" in hint for hint in hints)


def test_chat_trace_issue_context_includes_selected_previous_diff_and_hints():
    messages = [
        {"id": 1, "role": "user", "content": "지난주 리포트 정리"},
        {
            "id": 2,
            "role": "assistant",
            "content": "이전 답변",
            "metadata": {
                "status": "succeeded",
                "route": "vectordb",
                "search_filters": {"report_date_start": "2026-06-15"},
                "rerank_info": [{"file_name": "a.pdf"}],
            },
        },
        {"id": 3, "role": "user", "content": "개별 종목 자세히"},
        {
            "id": 4,
            "role": "assistant",
            "content": "현재 답변 [1]",
            "metadata": {
                "status": "succeeded",
                "route": "vectordb",
                "search_filters": {"report_date_start": "2026-06-15", "report_type": "company"},
                "scope_decision": {"reason": "matched_prior_section_alias"},
                "rerank_info": [{"rank": 1, "file_name": "company.pdf"}],
                "monitoring": {"retrieval": {"candidate_count_after_filter": 8}},
            },
        },
    ]
    thread = {"id": "thread-a", "name": "debug chat"}

    context = build_chat_trace_issue_context(thread, messages, selected_message_id=4)

    assert context["submitted_from"] == "chat_monitoring_trace"
    assert context["selected_user_question"] == "개별 종목 자세히"
    assert context["selected_message"]["id"] == 4
    assert context["previous_message"]["id"] == 2
    assert context["diff"]["search_filters"]["added"] == {"report_type": "company"}
    assert context["trace_detail"]["scope"]["search_filters"]["report_type"] == "company"
    assert "conversation_messages" not in context

def test_compact_graph_monitoring_metadata_keeps_route_filters_and_scores():
    metadata = compact_graph_monitoring_metadata(
        final_state={
            "route": "vectordb",
            "rewritten_query": "NAVER 최신 리포트",
            "uses_chat_history": False,
            "followup_scope_intent": False,
            "search_filters": {"target_name": "NAVER"},
            "temporal_context": None,
            "scope_source": "prior_search_scope",
            "routing_context": {"route_hint": "vectordb"},
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {"report_date_start": "2026-06-22"},
                "file_names": ["a.pdf", "b.pdf"],
            },
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
    assert metadata["scope_source"] == "prior_search_scope"
    assert metadata["routing_context"] == {"route_hint": "vectordb"}
    assert metadata["scope_decision"] == {"reason": "matched_prior_section_alias"}
    assert metadata["monitoring"]["state_trace"]["input"]["prior_search_scope"] == {
        "route": "vectordb",
        "search_filters": {"report_date_start": "2026-06-22"},
        "temporal_context": None,
        "scope_source": None,
        "file_count": 2,
        "file_names": ["a.pdf", "b.pdf"],
    }
    assert metadata["monitoring"]["retrieval"]["source_count"] == 2
    assert metadata["monitoring"]["retrieval"]["score_summary"]["rerank_score"]["avg"] == 0.7


def test_compact_graph_monitoring_metadata_handles_no_result_none_rerank_info():
    metadata = compact_graph_monitoring_metadata(
        final_state={
            "route": "vectordb",
            "search_filters": {
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-25",
                "report_type": "company",
            },
            "monitoring_metrics": {
                "retrieval": {
                    "candidate_count_after_filter": 0,
                }
            },
        },
        latency_seconds=1.2,
        rerank_info=None,
    )

    assert metadata["monitoring"]["retrieval"]["source_count"] == 0
    assert metadata["monitoring"]["retrieval"]["score_summary"] == {}
    assert metadata["monitoring"]["retrieval"]["candidate_count_after_filter"] == 0


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
        "운영 상태",
        "평가/실험",
        "이슈/회귀",
        "Chat Monitoring",
    ]


def test_global_monitoring_section_labels_group_related_pages():
    assert build_global_monitoring_section_labels("운영 상태") == ["데이터 상태", "임베딩 누락 문서", "전체 응답 품질"]
    assert build_global_monitoring_section_labels("평가/실험") == ["고정 평가셋", "실험 실행", "Parsing 비교"]
    assert build_global_monitoring_section_labels("이슈/회귀") == ["이슈 신고/회귀 후보"]

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
    assert candidate["triage_status"] == "new"
    assert candidate["operator_decision"] == "unreviewed"
    assert candidate["severity"] == "untriaged"
    assert candidate["recommended_next_step"] == "convert_to_evaluation_dataset_case"
    assert Path(candidate["json_path"]).exists()


def test_promote_trace_issue_report_to_eval_candidate_carries_draft_and_impact_area(tmp_path):
    report = {
        "id": "r2",
        "thread_id": "thread-b",
        "category": "Chat Monitoring trace",
        "created_at": "2026-06-22",
        "file_path": "debug/r2.txt",
        "content": "Finance LLM 문제 신고\nDescription:\n날짜 필터가 사라졌습니다.",
        "context": {
            "selected_user_question": "개별 종목에 대해 자세히 알려줘",
            "trace_detail": {
                "query_rewrite": {
                    "followup_scope_intent": True,
                    "scope_source": "prior_search_scope",
                    "scope_decision": {"reason": "matched_prior_section_alias"},
                },
                "scope": {"search_filters": {"report_type": "company", "report_date_start": "2026-06-09"}},
                "routing": {"route": "vectordb"},
                "sources": [
                    {"file_name": "naver-company.pdf", "report_type": "company", "rank": 1},
                    {"file_name": "naver-industry.pdf", "report_type": "industry", "rank": 2},
                ],
                "answer": {"citation_valid": True, "source_count": 2},
            },
            "debug_hints": ["⚠️ 직전 응답에는 날짜 필터가 있었는데 현재 응답에서 날짜 필터가 사라졌습니다."],
        },
    }

    candidate = promote_issue_report_to_eval_candidate(report, output_dir=tmp_path)

    assert candidate["impact_area"] == "filter_scope"
    assert candidate["eval_case_draft"]["question"] == "개별 종목에 대해 자세히 알려줘"
    assert candidate["eval_case_draft"]["expected_route"] == "vectordb"
    assert candidate["eval_case_draft"]["expected_filters"] == {"report_type": "company", "report_date_start": "2026-06-09"}
    assert candidate["eval_case_draft"]["expected_sources"] == [
        {"file_name": "naver-company.pdf", "report_type": "company"},
        {"file_name": "naver-industry.pdf", "report_type": "industry"},
    ]
    assert candidate["eval_case_draft"]["expected_state"]["scope_decision_reason"] == "matched_prior_section_alias"
    assert "filter_scope" in candidate["eval_case_draft"]["monitoring_dimensions"]


def test_build_eval_case_draft_from_issue_report_returns_none_without_trace_context():
    assert build_eval_case_draft_from_issue_report({"id": "r3", "content": "manual report"}) is None


def test_regression_candidate_helpers_list_rows_and_build_draft_dataset(tmp_path):
    candidate_a = promote_issue_report_to_eval_candidate(
        {
            "id": "r10",
            "thread_id": "thread-a",
            "category": "Chat Monitoring trace",
            "content": "출처 문제",
            "context": {
                "selected_user_question": "NAVER 요약",
                "trace_detail": {
                    "query_rewrite": {"original_question": "NAVER 요약"},
                    "scope": {"search_filters": {"target_name": "NAVER"}},
                    "routing": {"route": "vectordb"},
                    "sources": [{"file_name": "naver.pdf"}],
                },
            },
        },
        output_dir=tmp_path,
    )
    promote_issue_report_to_eval_candidate(
        {
            "id": "r11",
            "thread_id": "thread-b",
            "category": "답변 품질",
            "content": "manual only",
        },
        output_dir=tmp_path,
    )

    candidates = list_regression_candidates(tmp_path)
    rows = build_regression_candidate_rows(candidates)
    dataset = build_regression_candidate_dataset(candidates, selected_candidate_ids=[candidate_a["id"]])

    assert [candidate["id"] for candidate in candidates] == ["candidate_r11", "candidate_r10"]
    assert rows[0]["triage_status"] == "new"
    assert rows[1]["has_eval_case_draft"] is True
    assert dataset["name"] == "finance_llm_regression_candidate_dataset"
    assert dataset["cases"] == [candidate_a["eval_case_draft"]]


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

    run = run_evaluation_dataset(
        dataset,
        fake_invoke,
        output_dir=tmp_path,
        selected_case_ids=["case-b"],
        execution_mode="fixed_snapshot",
        data_source={"db_path": "snapshot/reports.db"},
    )

    assert seen_questions == ["B"]
    assert run["selected_case_ids"] == ["case-b"]
    assert run["summary"]["case_count"] == 1
    assert run["execution_mode"] == "fixed_snapshot"
    assert run["data_source"] == {"db_path": "snapshot/reports.db"}
    saved = Path(run["json_path"]).read_text(encoding="utf-8")
    assert '"execution_mode": "fixed_snapshot"' in saved


def test_filter_evaluation_runs_by_mode_does_not_compare_current_and_snapshot_runs():
    runs = [
        {"run_id": "old-current"},
        {"run_id": "new-current", "execution_mode": "current_data"},
        {"run_id": "snapshot", "execution_mode": "fixed_snapshot"},
    ]

    assert [run["run_id"] for run in filter_evaluation_runs_by_mode(runs, "current_data")] == [
        "old-current",
        "new-current",
    ]
    assert [run["run_id"] for run in filter_evaluation_runs_by_mode(runs, "fixed_snapshot")] == ["snapshot"]


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



def test_build_reusable_search_scope_carries_filters_files_and_sections():
    final_state = {
        "route": "vectordb",
        "search_filters": {"target_name": "NAVER", "report_date_start": "2026-06-09"},
        "temporal_context": {"report_date_start": "2026-06-09", "report_date_end": "2026-06-09"},
        "rerank_info": [
            {"file_name": "naver-company.pdf", "report_type": "company"},
            {"file_name": "naver-industry.pdf", "report_type": "industry"},
            {"file_name": "naver-company.pdf", "report_type": "company"},
        ],
    }

    scope = build_reusable_search_scope(final_state)

    assert scope["route"] == "vectordb"
    assert scope["search_filters"]["target_name"] == "NAVER"
    assert scope["file_names"] == ["naver-company.pdf", "naver-industry.pdf"]
    assert {section["id"] for section in scope["answer_scope_index"]["sections"]} == {"company", "industry"}


def test_evaluate_multiturn_turn_result_scores_context_and_expected_state():
    turn = {
        "id": "turn-2",
        "question": "?? ?? ??? ???",
        "expected_route": "vectordb",
        "expected_filters": {"target_name": "NAVER", "report_type": "company"},
        "expected_sources": [{"file_name": "naver-company.pdf"}],
        "expected_input": {"chat_history": True, "prior_search_scope": True},
        "expected_state": {
            "followup_scope_intent": True,
            "scope_source": "prior_search_scope",
            "scope_decision_reason": "matched_prior_section_alias",
        },
    }
    final_state = {
        "route": "vectordb",
        "search_filters": {"target_name": "NAVER", "report_type": "company"},
        "generation": "?? [1]",
        "rerank_info": [{"rank": 1, "file_name": "naver-company.pdf"}],
        "followup_scope_intent": True,
        "scope_source": "prior_search_scope",
        "scope_decision": {"reason": "matched_prior_section_alias"},
    }

    result = evaluate_multiturn_turn_result(
        turn,
        final_state,
        latency_seconds=0.1,
        input_had_chat_history=True,
        input_had_prior_search_scope=True,
    )

    assert result["status"] == "pass"
    assert result["chat_history_pass"] is True
    assert result["prior_scope_pass"] is True
    assert result["expected_state_pass"] is True


def test_run_multiturn_evaluation_dataset_carries_scope_and_thread_without_chat_history(tmp_path):
    dataset = {
        "name": "multiturn_eval",
        "version": 1,
        "cases": [
            {
                "id": "followup-scope",
                "description": "?? ??? ?? ?? ??? ?????",
                "turns": [
                    {
                        "id": "turn-1",
                        "question": "2026? 6? 9? NAVER ??? ??",
                        "expected_route": "vectordb",
                        "expected_filters": {"target_name": "NAVER"},
                        "expected_sources": [{"file_name": "naver-company.pdf"}],
                        "expected_input": {"chat_history": False, "prior_search_scope": False},
                    },
                    {
                        "id": "turn-2",
                        "question": "?? ?? ??? ???",
                        "expected_route": "vectordb",
                        "expected_filters": {"target_name": "NAVER", "report_type": "company"},
                        "expected_sources": [{"file_name": "naver-company.pdf"}],
                        "expected_input": {"chat_history": False, "prior_search_scope": True},
                        "expected_state": {
                            "followup_scope_intent": True,
                            "scope_source": "prior_search_scope",
                        },
                    },
                ],
            }
        ],
    }
    calls = []

    def fake_invoke(payload, config=None):
        calls.append({"payload": payload, "config": config})
        if len(calls) == 1:
            return {
                "route": "vectordb",
                "search_filters": {"target_name": "NAVER", "report_date_start": "2026-06-09"},
                "generation": "? ?? [1]",
                "rerank_info": [{"rank": 1, "file_name": "naver-company.pdf", "report_type": "company"}],
            }
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER", "report_type": "company"},
            "generation": "?? ?? [1]",
            "rerank_info": [{"rank": 1, "file_name": "naver-company.pdf", "report_type": "company"}],
            "followup_scope_intent": True,
            "scope_source": "prior_search_scope",
        }

    run = run_multiturn_evaluation_dataset(dataset, fake_invoke, output_dir=tmp_path)

    assert run["evaluation_type"] == "multiturn"
    assert run["summary"]["case_count"] == 1
    assert run["summary"]["turn_count"] == 2
    assert run["summary"]["passed"] == 1
    assert run["summary"]["turn_passed"] == 2
    assert "chat_history" not in calls[0]["payload"]
    assert "prior_search_scope" not in calls[0]["payload"]
    assert "chat_history" not in calls[1]["payload"]
    assert calls[1]["payload"]["prior_search_scope"]["search_filters"]["target_name"] == "NAVER"
    assert calls[1]["payload"]["prior_search_scope"]["file_names"] == ["naver-company.pdf"]
    assert calls[0]["config"]["configurable"]["thread_id"] == calls[1]["config"]["configurable"]["thread_id"]
    assert Path(run["json_path"]).exists()


def test_load_multiturn_evaluation_dataset_fixture_has_expected_mvp_cases():
    dataset = load_multiturn_evaluation_dataset()

    assert dataset["name"] == "finance_llm_multiturn_eval_dataset"
    assert len(dataset["cases"]) == 3
    assert all(case.get("type") == "multi_turn_chat" for case in dataset["cases"])
