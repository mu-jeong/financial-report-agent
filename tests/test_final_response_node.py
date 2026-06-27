from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import src.graphs.main_graph as main_graph


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
    assert result["chat_history"] == [
        ("사용자", "삼성전자 리포트와 현재 주가를 알려줘"),
        ("AI", "도구 결과를 반영한 최종 답변"),
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
        "chat_history": [("사용자", "저장된 리포트 수는?"), ("AI", "총 10건입니다.")],
    }


def test_vectordb_no_result_retries_once_without_memory():
    assert main_graph.should_retry_vectordb_without_memory(
        {"no_vector_results": True, "memory_retry_attempted": False}
    )
    assert not main_graph.should_retry_vectordb_without_memory(
        {"no_vector_results": True, "memory_retry_attempted": True}
    )


def test_build_graph_uses_memory_checkpointer_for_thread_state():
    app = main_graph.build_graph()

    assert app.checkpointer is not None


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
        }
    )

    assert result["rewritten_query"] == "삼성전자 최근 리포트 요약"
    assert "search_filters" not in result
    assert "temporal_context" not in result
    assert result["generation"] is None
    assert result["no_vector_results"] is False
    assert result["memory_retry_attempted"] is True
