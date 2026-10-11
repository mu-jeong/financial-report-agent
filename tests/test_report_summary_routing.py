import pytest
from langchain_core.runnables import RunnableLambda

from src.nodes import router, search_scope


def _fixed_company_filters(monkeypatch, filters=None):
    filters = filters or {"target_name": "삼성전자", "report_type": "company"}
    monkeypatch.setattr(search_scope, "infer_search_filters", lambda _text: dict(filters))
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda _text: None)
    monkeypatch.setattr(
        search_scope,
        "get_metadata_candidates",
        lambda: {"target_report_types": {
            target: ("company",)
            for target in [filters.get("target_name"), *(filters.get("target_names") or [])]
            if target
        }},
    )


@pytest.mark.parametrize(
    ("question", "rewritten_query"),
    [
        ("삼성전자 리포트를 정리해서 알려줘", "삼성전자 알려줘"),
        ("삼성전자 알려줘", "삼성전자 보고서를 취합해줘"),
        (
            "삼성전자 리포트 내용을 종합해서 알려줘",
            "삼성전자 리포트 내용을 종합해서 알려줘",
        ),
    ],
)
def test_report_synthesis_request_routes_to_report_body_without_llm(
    monkeypatch,
    question,
    rewritten_query,
):
    _fixed_company_filters(monkeypatch)

    def fail_if_llm_router_is_called(*_args, **_kwargs):
        raise AssertionError("report synthesis should deterministically use report bodies")

    monkeypatch.setattr(router, "build_chat_model", fail_if_llm_router_is_called)

    scope = search_scope.search_scope_node(
        {
            "question": question,
            "rewritten_query": rewritten_query,
            "followup_scope_intent": False,
            "prior_search_scope": None,
        }
    )
    route = router.router_node({"question": question, **scope})

    assert scope["routing_context"]["has_vector_intent"] is True
    assert scope["search_filters"] == {
        "target_name": "삼성전자",
        "report_type": "company",
    }
    assert route == {"route": "vectordb"}


@pytest.mark.parametrize("prior_route", [None, "rdb", "vectordb"])
@pytest.mark.parametrize("summary_word", ["정리", "요약", "취합", "종합"])
def test_multiple_company_report_summary_uses_bodies_even_after_metadata_answer(
    monkeypatch, prior_route, summary_word,
):
    targets = ["코스맥스", "삼성바이오로직스"]
    filters = {"target_names": targets, "report_type": "company"}
    _fixed_company_filters(monkeypatch, filters)
    question = f"코스맥스, 삼성바이오로직스 리포트 {summary_word}해줘"

    def fail_if_llm_router_is_called(*_args, **_kwargs):
        raise AssertionError("company report summaries must retrieve report bodies")

    monkeypatch.setattr(router, "build_chat_model", fail_if_llm_router_is_called)
    prior_dates = {"report_date_start": "2026-10-06", "report_date_end": "2026-10-08"}
    prior_scope = {"route": prior_route, "search_filters": prior_dates} if prior_route else None
    scope = search_scope.search_scope_node({
        "question": question,
        "rewritten_query": "코스맥스, 삼성바이오로직스 리포트",
        "prior_search_scope": prior_scope,
    })

    assert scope["routing_context"]["has_vector_intent"] is True
    assert scope["routing_context"]["route_hint"] != "rdb"
    assert router.router_node({"question": question, **scope}) == {"route": "vectordb"}
    assert scope["search_filters"] == {**(prior_dates if prior_route else {}), **filters}
    assert scope["retrieval_plan"]["type"] == "company_comparison"
    assert scope["retrieval_plan"]["target_names"] == targets


@pytest.mark.parametrize("targets", [["삼성전자"], ["코스맥스", "삼성바이오로직스"]])
@pytest.mark.parametrize("content_request", ["리포트 정리해줘", "리포트 주요 내용을 알려줘"])
def test_full_period_content_request_overrides_prior_metadata_route(monkeypatch, targets, content_request):
    filters = {"report_type": "company", "target_name": targets[0]} if len(targets) == 1 else {
        "report_type": "company", "target_names": targets,
    }
    _fixed_company_filters(monkeypatch, filters)
    question = f"{', '.join(targets)} 전체 기간 {content_request}"
    scope = search_scope.search_scope_node({
        "question": question, "rewritten_query": question,
        "prior_search_scope": {
            "route": "rdb",
            "search_filters": {**filters, "report_date_start": "2026-10-06", "report_date_end": "2026-10-08"},
            "file_names": ["previous-metadata-result.pdf"],
        },
    })

    assert scope["search_filters"] == filters
    assert scope["routing_context"]["has_vector_intent"] is True
    assert scope["routing_context"]["route_hint"] != "rdb"
    assert router.router_node({"question": question, **scope}) == {"route": "vectordb"}


@pytest.mark.parametrize(
    "question",
    [
        "삼성전자 리포트 목록 정리해줘",
        "삼성전자 보고서 발간일 정리해줘",
        "삼성전자 리포트 개수 정리해줘",
        "삼성전자 리포트 제목 정리해줘",
    ],
)
@pytest.mark.parametrize("targets", [["삼성전자"], ["코스맥스", "삼성바이오로직스"]])
def test_report_metadata_summary_is_not_forced_to_report_body(
    monkeypatch,
    question,
    targets,
):
    filters = {"report_type": "company", "target_name": targets[0]} if len(targets) == 1 else {
        "report_type": "company", "target_names": targets,
    }
    _fixed_company_filters(monkeypatch, filters)
    question = question.replace("삼성전자", ", ".join(targets))
    classifier_calls = []

    class FakeChatModel:
        def with_structured_output(self, _schema):
            return RunnableLambda(
                lambda _prompt: (
                    classifier_calls.append(question)
                    or router.RouteDecision(route="rdb")
                )
            )

    monkeypatch.setattr(router, "build_chat_model", lambda **_kwargs: FakeChatModel())

    scope = search_scope.search_scope_node(
        {
            "question": question,
            "rewritten_query": question,
            "followup_scope_intent": False,
            "prior_search_scope": None,
        }
    )
    route = router.router_node({"question": question, **scope})

    assert scope["routing_context"]["has_vector_intent"] is False
    assert classifier_calls == [question]
    assert route == {"route": "rdb"}


def test_explicit_content_request_wins_over_metadata_wording(monkeypatch):
    _fixed_company_filters(monkeypatch)

    def fail_if_llm_router_is_called(*_args, **_kwargs):
        raise AssertionError("explicit content intent should bypass the LLM router")

    monkeypatch.setattr(router, "build_chat_model", fail_if_llm_router_is_called)
    question = "삼성전자 리포트 목록에서 주요 내용을 정리해줘"

    scope = search_scope.search_scope_node(
        {
            "question": question,
            "rewritten_query": question,
            "followup_scope_intent": False,
            "prior_search_scope": None,
        }
    )
    route = router.router_node({"question": question, **scope})

    assert scope["routing_context"]["has_vector_intent"] is True
    assert route == {"route": "vectordb"}


@pytest.mark.parametrize("summary_word", ["정리", "요약", "취합", "종합"])
def test_long_period_company_report_summary_uses_temporal_retrieval_plan(
    summary_word,
):
    plan = search_scope.build_retrieval_plan(
        f"삼성전자 리포트를 {summary_word}해줘",
        {
            "target_name": "삼성전자",
            "report_type": "company",
            "report_date_start": "2026-01-01",
            "report_date_end": "2026-12-31",
        },
        has_vector_intent=True,
    )

    assert plan == {
        "type": "temporal_report_set_summary",
        "preflight": "rdb_file_universe",
        "bucket_by": "month",
    }
