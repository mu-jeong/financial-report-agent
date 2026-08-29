from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Overwrite

import src.graphs.main_graph as main_graph


def _document_contract_sources():
    return [
        {"rank": 1, "passage_rank": 1, "document_rank": 1, "file_name": "first.pdf"},
        {"rank": 2, "passage_rank": 2, "document_rank": 1, "file_name": "first.pdf"},
        {"rank": 3, "passage_rank": 3, "document_rank": 2, "file_name": "second.pdf"},
    ]


def _document_contract():
    return {
        "version": 2,
        "rank_kind": "document",
        "passage_count": 3,
        "document_count": 2,
    }


class FakeChatModel:
    def __init__(self, *args, **kwargs):
        pass

    def invoke(self, messages):
        assert isinstance(messages[0], HumanMessage)
        assert isinstance(messages[-1], ToolMessage)
        return AIMessage(content="도구 결과를 반영한 최종 답변")


def test_final_response_node_returns_only_new_message_delta(monkeypatch):
    monkeypatch.setattr(
        "src.llms.factory.build_chat_model",
        lambda temperature=0.2, **kwargs: FakeChatModel(),
    )
    existing_messages = [
        HumanMessage(content="원 질문과 검색 문맥"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_stock_price",
                    "args": {"company_name": "삼성전자"},
                    "id": "call-1",
                }
            ],
        ),
        ToolMessage(content="최근 주가 데이터", tool_call_id="call-1"),
    ]

    result = main_graph.final_response_node(
        {
            "question": "삼성전자 리포트와 현재 주가를 알려줘",
            "messages": existing_messages,
        }
    )

    assert result["generation"] == "도구 결과를 반영한 최종 답변"
    assert result["messages"] == [AIMessage(content="도구 결과를 반영한 최종 답변")]
    assert result["monitoring_metrics"]["generation"]["call_count"] == 1
    assert result["monitoring_metrics"]["generation"]["calls"][0]["phase"] == (
        "tool_followup_answer"
    )
    assert "chat_history" not in result


def test_final_response_node_merges_tool_followup_with_initial_generation(monkeypatch):
    monkeypatch.setattr(
        "src.llms.factory.build_chat_model",
        lambda temperature=0.2, **kwargs: FakeChatModel(),
    )
    initial_call = {
        "streamed": False,
        "request_ns": 100,
        "after_first_token_ns": None,
        "time_to_first_token_ns": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cache_read_tokens": None,
        "reasoning_tokens": None,
        "provider_name": None,
        "gateway_provider": None,
        "model_name": None,
        "router_metadata_status": "unsupported",
        "phase": "vectordb_answer",
    }
    result = main_graph.final_response_node(
        {
            "question": "주가까지 반영해줘",
            "messages": [
                HumanMessage(content="검색 문맥"),
                ToolMessage(content="최근 주가", tool_call_id="call-1"),
            ],
            "monitoring_metrics": {
                "retrieval": {"source_count": 1},
                "generation": {"calls": [initial_call]},
            },
        }
    )

    assert result["monitoring_metrics"]["retrieval"] == {"source_count": 1}
    generation = result["monitoring_metrics"]["generation"]
    assert generation["call_count"] == 2
    assert [call["phase"] for call in generation["calls"]] == [
        "vectordb_answer",
        "tool_followup_answer",
    ]


def test_final_response_node_uses_existing_generation_without_messages():
    result = main_graph.final_response_node(
        {
            "question": "저장된 리포트 수는?",
            "generation": "총 10건입니다.",
            "messages": [HumanMessage(content="기존 메시지")],
        }
    )

    assert result == {
        "generation": "총 10건입니다.",
    }


def test_final_response_uses_validated_document_count_for_v2_cleanup():
    contract = _document_contract()
    result = main_graph.final_response_node(
        {
            "question": "summarize",
            "generation": "first [1], second [2], unavailable [3]",
            "rerank_info": _document_contract_sources(),
            "citation_contract": contract,
        }
    )

    assert result["generation"] == "first [1], second [2], unavailable "
    assert result["citation_contract"] == contract


def test_final_response_removes_future_out_of_range_document_heading():
    result = main_graph.final_response_node(
        {
            "question": "summarize",
            "generation": "### 문서 7: 보수적 관점 [2]",
            "rerank_info": _document_contract_sources(),
            "citation_contract": _document_contract(),
        }
    )

    assert result["generation"] == "### 보수적 관점 [2]"


def test_final_response_keeps_legacy_document_heading_unchanged():
    result = main_graph.final_response_node(
        {
            "question": "summarize",
            "generation": "문서 7: historical label [7]",
            "rerank_info": [
                {"rank": rank, "file_name": file_name}
                for rank, file_name in enumerate(
                    ["1.pdf", "2.pdf", "3.pdf", "4.pdf", "same.pdf", "6.pdf", "same.pdf"],
                    1,
                )
            ],
        }
    )

    assert result["generation"] == "문서 7: historical label [7]"


def test_final_response_does_not_rewrite_invalid_marked_contract():
    invalid_contract = {
        "version": 2,
        "rank_kind": "document",
        "passage_count": 99,
        "document_count": 2,
    }
    result = main_graph.final_response_node(
        {
            "question": "summarize",
            "generation": "keep diagnostic citation [3]",
            "rerank_info": _document_contract_sources(),
            "citation_contract": invalid_contract,
        }
    )

    assert result["generation"] == "keep diagnostic citation [3]"
    assert result["citation_contract"] == invalid_contract


def test_tool_followup_preserves_valid_document_contract(monkeypatch):
    monkeypatch.setattr(
        "src.llms.factory.build_chat_model",
        lambda temperature=0.2, **kwargs: FakeChatModel(),
    )
    contract = _document_contract()
    result = main_graph.final_response_node(
        {
            "question": "include price",
            "messages": [
                HumanMessage(content="report evidence"),
                ToolMessage(content="price", tool_call_id="call-1"),
            ],
            "rerank_info": _document_contract_sources(),
            "citation_contract": contract,
        }
    )

    assert result["citation_contract"] == contract


def test_vectordb_no_result_retries_once_without_memory():
    assert main_graph.should_retry_vectordb_without_memory(
        {"no_vector_results": True, "memory_retry_attempted": False}
    )
    assert not main_graph.should_retry_vectordb_without_memory(
        {"no_vector_results": True, "memory_retry_attempted": True}
    )
    assert main_graph.should_retry_vectordb_without_memory(
        {
            "no_vector_results": True,
            "memory_retry_attempted": False,
            "vector_retryable": True,
        }
    )
    assert not main_graph.should_retry_vectordb_without_memory(
        {
            "no_vector_results": True,
            "memory_retry_attempted": False,
            "vector_retryable": False,
        }
    )


def test_build_graph_uses_memory_checkpointer_for_thread_state():
    app = main_graph.build_graph()

    assert app.checkpointer is not None
    assert "vector_dispatcher" in app.get_graph().nodes
    assert "company_comparison" in app.get_graph().nodes
    assert "company_comparison_sequential" not in app.get_graph().nodes


def test_too_many_targets_only_exposes_its_reachable_graph_route():
    graph = main_graph.build_graph().get_graph(xray=True)

    targets = {
        edge.target
        for edge in graph.edges
        if edge.source == "too_many_targets"
    }

    assert targets == {"final_response_node"}


def test_final_response_node_commits_active_scope_for_next_turn():
    result = main_graph.final_response_node(
        {
            "question": "삼성전자 리포트 요약",
            "generation": "요약 답변 [1]",
            "route": "vectordb",
            "search_filters": {"target_name": "삼성전자", "report_type": "company"},
            "temporal_context": {"report_date_start": "2026-06-22", "report_date_end": "2026-06-25"},
            "scope_source": "explicit_question",
            "rerank_info": [{"file_name": "samsung.pdf", "rank": 1, "report_type": "company"}],
        }
    )

    assert result["active_scope"]["route"] == "vectordb"
    assert result["active_scope"]["search_filters"] == {"target_name": "삼성전자", "report_type": "company"}
    assert result["active_scope"]["temporal_context"] == {"report_date_start": "2026-06-22", "report_date_end": "2026-06-25"}
    assert result["active_scope"]["file_names"] == ["samsung.pdf"]


def test_final_response_node_does_not_commit_no_result_scope():
    result = main_graph.final_response_node(
        {
            "question": "SK하이닉스 상세",
            "generation": "지정된 조건에 맞는 임베딩 완료 리포트를 찾지 못했습니다.",
            "route": "vectordb",
            "search_filters": {
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-06",
                "target_name": "SK하이닉스",
                "report_type": "company",
            },
            "no_vector_results": True,
            "rerank_info": [],
        }
    )

    assert "active_scope" not in result


def test_clear_short_term_memory_retry_keeps_metadata_filters():
    result = main_graph.clear_short_term_memory_retry_node(
        {
            "question": "삼성전자 최근 리포트 요약",
            "rewritten_query": "이전 대화 맥락이 섞인 검색어",
            "search_filters": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-21",
                "target_name": "현대차",
                "report_type": "company",
            },
            "temporal_context": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-21",
            },
            "generation": "지정된 조건에 맞는 임베딩 완료 리포트를 찾지 못했습니다.",
            "messages": [HumanMessage(content="stale")],
            "vector_attempt_id": 0,
            "citation_contract": _document_contract(),
        }
    )

    assert result["rewritten_query"] == "삼성전자 최근 리포트 요약"
    assert "search_filters" not in result
    assert "temporal_context" not in result
    assert result["generation"] is None
    assert result["no_vector_results"] is False
    assert result["memory_retry_attempted"] is True
    assert result["vector_attempt_id"] == 1
    assert result["messages"] == Overwrite([])
    assert result["citation_contract"] is None


def test_turn_prepare_resets_only_volatile_turn_state(monkeypatch):
    monkeypatch.setattr(main_graph, "uuid4", lambda: "run-2")

    result = main_graph.turn_prepare_node(
        {
            "question": "새 질문",
            "active_scope": {"search_filters": {"target_name": "A"}},
            "generation": "이전 답변",
            "messages": [HumanMessage(content="이전 도구 문맥")],
            "retrieval_plan": {"type": "old"},
            "industry_lookup_context": {"company_names": ["old"]},
            "rdb_sources": [{"file_name": "old.pdf"}],
            "rdb_query_shape": {"type": "count_by_target"},
            "rdb_missing_targets": ["old"],
            "citation_contract": _document_contract(),
        }
    )

    assert result["messages"] == Overwrite([])
    assert result["generation"] is None
    assert result["retrieval_plan"] is None
    assert result["industry_lookup_context"] is None
    assert result["rdb_sources"] is None
    assert result["rdb_query_shape"] is None
    assert result["rdb_missing_targets"] is None
    assert result["citation_contract"] is None
    assert result["vector_run_id"] == "run-2"
    assert result["vector_attempt_id"] == 0
    assert "active_scope" not in result


def test_vector_dispatcher_always_selects_send_and_scope_reduction():
    base = {
        "search_filters": {"target_names": ["A", "B"]},
        "retrieval_plan": {
            "type": "company_comparison",
            "target_names": ["A", "B"],
            "execution_mode": "send",
        },
    }
    assert main_graph.select_vector_execution(base) == "company_comparison"

    legacy_reference = {
        **base,
        "retrieval_plan": {
            **base["retrieval_plan"],
            "execution_mode": "sequential_reference",
        },
    }
    assert main_graph.select_vector_execution(legacy_reference) == "company_comparison"

    too_many = {
        "retrieval_plan": {
            "type": "too_many_targets",
            "target_names": list("ABCDEF"),
            "max_targets": 5,
        }
    }
    assert main_graph.select_vector_execution(too_many) == "too_many_targets"
    result = main_graph.too_many_targets_node(too_many)
    assert result["vector_outcome"] == "too_many_targets"
    assert result["vector_retryable"] is False
    assert result["citation_contract"] is None
    assert "최대 5개" in result["generation"]


def test_retry_clears_pinned_revision_but_preserves_comparison_plan():
    result = main_graph.clear_short_term_memory_retry_node(
        {
            "question": "A와 B 비교",
            "vector_attempt_id": 4,
            "retrieval_plan": {
                "type": "company_comparison",
                "target_names": ["A", "B"],
                "comparison_id": "run-1",
                "attempt_id": "4",
                "expected_revision": {"snapshot_id": "old"},
                "execution_mode": "send",
            },
        }
    )

    assert result["vector_attempt_id"] == 5
    assert result["retrieval_plan"] == {
        "type": "company_comparison",
        "target_names": ["A", "B"],
        "comparison_id": "run-1",
        "execution_mode": "send",
    }


def test_final_response_always_names_missing_rdb_targets():
    result = main_graph.final_response_node(
        {
            "question": "A와 B 최신 리포트",
            "generation": "A 리포트만 조회됐습니다.",
            "rdb_missing_targets": ["B"],
        }
    )

    assert result["generation"].endswith("조회 결과가 없는 기업: B")
