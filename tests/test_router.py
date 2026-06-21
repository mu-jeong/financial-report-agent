from src.nodes import router


def test_router_forces_vectordb_for_report_content_intent(monkeypatch):
    def fail_if_llm_router_is_called(*args, **kwargs):
        raise AssertionError("LLM router should not be called for content-analysis intent")

    monkeypatch.setattr(router, "build_chat_model", fail_if_llm_router_is_called)

    result = router.router_node(
        {
            "question": "???? ??? ???? ??? ?? ????? ?? ???",
            "rewritten_query": "???? ??? ???? ??? ?? ????? ?? ???",
            "routing_context": {"has_vector_intent": True},
        }
    )

    assert result == {"route": "vectordb"}


def test_router_uses_route_hint_before_llm(monkeypatch):
    def fail_if_llm_router_is_called(*args, **kwargs):
        raise AssertionError("LLM router should not be called when route_hint exists")

    monkeypatch.setattr(router, "build_chat_model", fail_if_llm_router_is_called)

    result = router.router_node(
        {
            "question": "????",
            "rewritten_query": "????",
            "routing_context": {"route_hint": "rdb", "has_vector_intent": False},
        }
    )

    assert result == {"route": "rdb"}
