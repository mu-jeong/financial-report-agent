from langchain_core.documents import Document
from langchain_core.messages import AIMessage, ToolMessage

import src.graphs.main_graph as main_graph
from src.nodes import vectordb_comparison as comparison


REVISION = {
    "snapshot_id": "snapshot-1",
    "publication_generation": 2,
    "delta_generation": 0,
    "profile_id": "profile-1",
}


def _comparison_plan() -> dict:
    return {
        "type": "company_comparison",
        "target_names": ["A", "B"],
        "shared_filters": {"report_type": "company"},
        "union_candidate_limit": 60,
        "final_budget": 20,
        "expected_revision": REVISION,
    }


def _patch_main_pipeline(monkeypatch) -> None:
    monkeypatch.setattr(
        main_graph,
        "query_rewrite_node",
        lambda state: {"rewritten_query": f"{state['question']} rewritten"},
    )
    monkeypatch.setattr(
        main_graph,
        "search_scope_prepare_node",
        lambda _state: {"scope_prepare": {}},
    )
    monkeypatch.setattr(main_graph, "industry_lookup_node", lambda _state: {})
    monkeypatch.setattr(
        main_graph,
        "search_scope_merge_node",
        lambda _state: {
            "search_filters": {
                "target_names": ["A", "B"],
                "report_type": "company",
            },
            "routing_context": {"has_vector_intent": True},
            "retrieval_plan": _comparison_plan(),
        },
    )
    monkeypatch.setattr(main_graph, "router_node", lambda _state: {"route": "vectordb"})
    monkeypatch.setattr(
        main_graph,
        "vectordb_scope_preflight_node",
        lambda _state: {},
    )


def _document(target: str) -> Document:
    return Document(
        page_content=f"{target} evidence",
        metadata={
            "target_name": target,
            "title": f"{target} report",
            "file_name": f"{target}.pdf",
            "report_type": "company",
            "chunk_uid": f"chunk-{target}",
            **REVISION,
        },
    )


def test_main_graph_retries_mixed_revision_through_dispatcher_once(
    monkeypatch,
):
    _patch_main_pipeline(monkeypatch)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    retrieval_calls = {"A": 0, "B": 0}
    synthesis_calls = []

    def retrieve(_query, filters):
        target = filters["target_name"]
        retrieval_calls[target] += 1
        revision = dict(REVISION)
        if target == "B" and retrieval_calls[target] == 1:
            revision["publication_generation"] = 3
        return [(_document(target), 1.0)], {"revision": revision}

    def synthesize(_question, _query, candidates, missing):
        synthesis_calls.append((len(candidates), list(missing)))
        return "comparison [1] [2]", [AIMessage(content="comparison")]

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    monkeypatch.setattr(
        comparison.vectordb,
        "get_active_retrieval_revision",
        lambda: REVISION,
    )
    monkeypatch.setattr(comparison, "_synthesize_answer", synthesize)

    app = main_graph.build_graph()
    result = app.invoke(
        {"question": "A와 B를 비교해줘"},
        config={"configurable": {"thread_id": "retry-comparison"}},
    )

    assert retrieval_calls == {"A": 2, "B": 2}
    assert synthesis_calls == [(2, [])]
    assert result["generation"] == "comparison [1] [2]"
    assert result["vector_attempt_id"] == 1
    assert result["memory_retry_attempted"] is True
    assert result["vector_outcome"] == "complete"
    assert result["monitoring_metrics"]["comparison"]["attempt_id"] == "1"


def test_main_graph_partial_result_does_not_retry_and_turn_state_is_reset(
    monkeypatch,
):
    _patch_main_pipeline(monkeypatch)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    retrieval_calls = {"A": 0, "B": 0}

    def retrieve(_query, filters):
        target = filters["target_name"]
        retrieval_calls[target] += 1
        docs = [] if target == "B" else [(_document(target), 1.0)]
        return docs, {"revision": REVISION}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)

    app = main_graph.build_graph()
    config = {"configurable": {"thread_id": "partial-comparison"}}
    first = app.invoke({"question": "first"}, config=config)
    second = app.invoke({"question": "second"}, config=config)

    assert retrieval_calls == {"A": 2, "B": 2}
    assert first["vector_outcome"] == second["vector_outcome"] == "insufficient"
    assert second["vector_attempt_id"] == 0
    assert second["memory_retry_attempted"] is False
    assert second["messages"] == []
    assert first["vector_run_id"] != second["vector_run_id"]
    assert [item["target_name"] for item in second["rerank_info"]] == ["A"]


def test_same_thread_does_not_replay_previous_stock_tool_call(monkeypatch):
    _patch_main_pipeline(monkeypatch)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    tool_calls = []
    final_messages = []
    synthesis_count = 0

    def retrieve(_query, filters):
        target = filters["target_name"]
        return [(_document(target), 1.0)], {"revision": REVISION}

    def synthesize(_question, _query, _candidates, _missing):
        nonlocal synthesis_count
        synthesis_count += 1
        if synthesis_count == 1:
            return None, [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_stock_price",
                            "args": {"company_name": "A"},
                            "id": "call-1",
                        }
                    ],
                )
            ]
        return "second answer", [AIMessage(content="second turn")]

    def fake_stock_tool(state):
        tool_calls.append(state["messages"][-1].tool_calls[0]["id"])
        return {
            "messages": [
                ToolMessage(content="price", tool_call_id="call-1")
            ]
        }

    def fake_final(state):
        final_messages.append(list(state.get("messages") or []))
        return {"generation": state.get("generation") or "tool folded"}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    monkeypatch.setattr(comparison, "_synthesize_answer", synthesize)
    monkeypatch.setattr(main_graph, "stock_price_tool_node", fake_stock_tool)
    monkeypatch.setattr(main_graph, "final_response_node", fake_final)

    app = main_graph.build_graph()
    config = {"configurable": {"thread_id": "stock-tool-reset"}}
    first = app.invoke({"question": "first"}, config=config)
    second = app.invoke({"question": "second"}, config=config)

    assert first["generation"] == "tool folded"
    assert second["generation"] == "second answer"
    assert tool_calls == ["call-1"]
    assert len(final_messages[0]) == 2
    assert len(final_messages[1]) == 1
    assert final_messages[1][0].content == "second turn"
