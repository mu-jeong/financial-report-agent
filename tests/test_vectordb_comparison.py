import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from random import Random

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from src.nodes import vectordb_comparison as comparison


def _state(**extra):
    state = {
        "question": "A와 B를 비교해줘",
        "rewritten_query": "A B 비교",
        "search_filters": {"target_names": ["A", "B"], "broker": "X"},
        "retrieval_plan": {
            "type": "company_comparison",
            "target_names": ["A", "B"],
            "comparison_id": "cmp",
            "attempt_id": "1",
            "expected_revision": {"snapshot_id": "s", "publication_generation": 2},
        },
    }
    state.update(extra)
    return state


def _install_fakes(monkeypatch):
    def retrieve(query, filters):
        target = filters["target_name"]
        docs = [
            (Document(page_content=f"{target}-{index}", metadata={
                "target_name": target, "title": f"{target} report {index}",
                "chunk_uid": f"{target}-{index}", "snapshot_id": "s",
                "publication_generation": 2, "broker": "X",
            }), float(index))
            for index in range(3)
        ]
        return docs, {"snapshot_id": "s", "publication_generation": 2}

    calls = {"synthesis": 0}

    def synthesize(question, query, candidates, missing, annotated_sources=None, citation_contract=None):
        calls["synthesis"] += 1
        return "answer " + " ".join(f"[{i}]" for i in range(1, len(candidates) + 1)), [AIMessage(content="answer")]

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    monkeypatch.setattr(comparison, "_synthesize_answer", synthesize)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    return calls


def test_plan_is_ordered_bounded_and_uses_singular_branch_filters():
    state = _state()
    state["search_filters"]["file_names"] = ["stale.pdf"]
    plan = comparison.build_comparison_plan(state)
    assert plan["targets"] == ["A", "B"]
    assert plan["candidate_budget_per_target"] == 30
    branches = comparison._branch_inputs(plan)
    assert [branch["target_name"] for branch in branches] == ["A", "B"]
    assert all("target_names" not in branch["filters"] for branch in branches)
    assert all("file_names" not in branch["filters"] for branch in branches)
    assert [branch["filters"]["target_name"] for branch in branches] == ["A", "B"]
    assert [branch["branch_query"] for branch in branches] == ["A 비교", "B 비교"]
    assert all(len(branch["branch_query_sha256"]) == 64 for branch in branches)


def test_latest_plan_pins_one_selected_file_per_target(monkeypatch):
    state = _state()
    state["retrieval_plan"].update(
        {
            "selection_mode": "latest_per_target",
            "candidate_budget_per_target": 4,
            "union_candidate_limit": 8,
            "final_budget": 8,
        }
    )
    monkeypatch.setattr(
        comparison.vectordb,
        "fetch_latest_reports_by_target",
        lambda targets, filters: {
            "A": {
                "target_name": "A",
                "report_date": "2026-08-10",
                "title": "A latest",
                "broker": "X",
                "file_name": "a-latest.pdf",
            },
            "B": {
                "target_name": "B",
                "report_date": "2026-08-09",
                "title": "B latest",
                "broker": "X",
                "file_name": "b-latest.pdf",
            },
        },
    )

    plan = comparison.build_comparison_plan(state)
    branches = comparison._branch_inputs(plan)

    assert plan["selection_mode"] == "latest_per_target"
    assert plan["retrieval_concurrency_limit"] == 2
    assert [branch["filters"]["file_names"] for branch in branches] == [
        ["a-latest.pdf"],
        ["b-latest.pdf"],
    ]
    assert comparison._output_filters(plan)["file_names"] == [
        "a-latest.pdf",
        "b-latest.pdf",
    ]

    candidates = [
        {
            "target_name": branch["target_name"],
            "meta": {
                "file_name": branch["filters"]["file_names"][0],
                "report_date": branch["selected_report"]["report_date"],
            },
        }
        for branch in branches
    ]
    latest_metrics = comparison._latest_selection_metrics(
        plan,
        candidates,
        "A [1] B [2]",
    )
    assert latest_metrics["status"] == "complete"
    assert latest_metrics["context_target_count"] == 2
    assert latest_metrics["cited_target_count"] == 2


def test_branch_query_removes_only_other_target_and_has_stable_hash():
    first = comparison.build_target_branch_query(
        "이번 주 삼성전자와 SK 하이닉스 리포트 요약 비교",
        "SK하이닉스",
        ["삼성전자", "SK하이닉스"],
    )
    second = comparison.build_target_branch_query(
        "이번 주 삼성전자와 SK 하이닉스 리포트 요약 비교",
        "SK하이닉스",
        ["삼성전자", "SK하이닉스"],
    )

    assert first == second
    assert "SK하이닉스" in first[0]
    assert "삼성전자" not in first[0]
    assert "이번 주" in first[0]
    assert "리포트 요약 비교" in first[0]


def test_branch_query_removes_empty_multi_target_separators():
    query, _digest = comparison.build_target_branch_query(
        "A, B, C, D, E 의 최신 리포트를 각각 요약해줘",
        "C",
        ["A", "B", "C", "D", "E"],
    )

    assert query == "C 최신 리포트를 요약해줘"


def test_dispatch_returns_one_minimal_send_per_target():
    plan = comparison.build_comparison_plan(_state())
    sends = comparison.dispatch_company_retrieval({"comparison_plan": plan})

    assert [send.node for send in sends] == ["retrieve_company", "retrieve_company"]
    assert [send.arg["target_name"] for send in sends] == ["A", "B"]
    assert [send.arg["target_index"] for send in sends] == [0, 1]
    assert all("messages" not in send.arg and "generation" not in send.arg for send in sends)


def test_plan_uses_turn_run_and_attempt_when_plan_does_not_override_them():
    state = _state(vector_run_id="run-7", vector_attempt_id=0)
    state["retrieval_plan"].pop("comparison_id")
    state["retrieval_plan"].pop("attempt_id")

    plan = comparison.build_comparison_plan(state)

    assert plan["comparison_id"] == "run-7"
    assert plan["attempt_id"] == "0"


def test_plan_pins_active_revision_when_caller_did_not_supply_one(monkeypatch):
    state = _state()
    state["retrieval_plan"].pop("expected_revision")
    monkeypatch.setattr(
        comparison.vectordb,
        "get_active_retrieval_revision",
        lambda: {
            "snapshot_id": "active",
            "publication_generation": 3,
            "delta_generation": 1,
            "profile_id": "p",
        },
    )
    plan = comparison.build_comparison_plan(state)
    assert plan["expected_revision"] == {
        "snapshot_id": "active",
        "publication_generation": 3,
        "delta_generation": 1,
        "profile_id": "p",
    }


def test_revision_preflight_failure_becomes_retryable_result(monkeypatch):
    state = _state()
    state["retrieval_plan"].pop("expected_revision")
    synthesis_calls = _install_fakes(monkeypatch)
    retrieval_calls = []
    monkeypatch.setattr(
        comparison.vectordb,
        "get_active_retrieval_revision",
        lambda: (_ for _ in ()).throw(
            comparison.vectordb.RetrievalDispatchError("revision unavailable")
        ),
    )
    monkeypatch.setattr(
        comparison.vectordb,
        "_retrieve_docs_with_scores",
        lambda *_args: retrieval_calls.append(True),
    )

    result = comparison.build_company_comparison_subgraph().invoke(state)

    assert result["vector_outcome"] == "all_failed"
    assert result["vector_retryable"] is True
    assert result["monitoring_metrics"]["comparison"]["preflight_error_code"] == (
        "RetrievalDispatchError"
    )
    assert retrieval_calls == []
    assert synthesis_calls["synthesis"] == 0


def test_plan_rejects_silent_target_truncation():
    state = _state()
    state["retrieval_plan"] = {"target_names": list("ABCDEF")}
    try:
        comparison.build_comparison_plan(state)
    except ValueError as exc:
        assert "2 and 5" in str(exc)
    else:
        raise AssertionError("six targets must be rejected")


def test_keyed_reducer_upserts_retry_result():
    key = ("cmp", "1", "A")
    old = {key: {"status": "failed"}}
    new = {key: {"status": "success"}}
    assert comparison.keyed_upsert_results(old, new)[key]["status"] == "success"
    assert comparison.keyed_upsert_results(new, new) == new


def test_keyed_reducer_is_order_independent_for_distinct_branches():
    left = {("cmp", "1", "A"): {"status": "success"}}
    right = {("cmp", "1", "B"): {"status": "no_result"}}

    assert comparison.keyed_upsert_results(left, right) == comparison.keyed_upsert_results(
        right, left
    )


def test_worker_is_exact_compact_and_converts_expected_failure(monkeypatch):
    long_text = "가" * 5000

    def retrieve(query, filters):
        return [
            (Document(page_content=long_text, metadata={"target_name": "A", "chunk_uid": "a", "broker": "X"}), 1),
            (Document(page_content="wrong", metadata={"target_name": "B", "chunk_uid": "b", "broker": "X"}), 2),
        ], {"snapshot_id": "s", "publication_generation": 2}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    plan = comparison.build_comparison_plan(_state())
    result = next(iter(comparison.retrieve_company(comparison._branch_inputs(plan)[0])["branch_results"].values()))
    assert result["status"] == "success"
    assert len(result["candidates"]) == 1
    assert len(result["candidates"][0]["text"].encode("utf-8")) <= 4096
    assert set(result["actual_revision"]) == {"snapshot_id", "publication_generation", "delta_generation", "profile_id"}
    assert result["metrics"]["branch_query"] == "A 비교"
    assert len(result["metrics"]["branch_query_sha256"]) == 64
    json.dumps(result)

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", lambda *_: (_ for _ in ()).throw(comparison.vectordb.RetrievalDispatchError("down")))
    failed = next(iter(comparison.retrieve_company(comparison._branch_inputs(plan)[0])["branch_results"].values()))
    assert failed["status"] == "failed"
    assert "RetrievalDispatchError" in failed["error"]


def test_compiled_send_matches_sequential_reference(monkeypatch):
    calls = _install_fakes(monkeypatch)
    sequential = comparison.run_sequential_reference(_state())
    sent = comparison.build_company_comparison_subgraph().invoke(_state())
    for key in ("generation", "rerank_info", "no_vector_results"):
        assert sent[key] == sequential[key]
    assert [item["target_name"] for item in sent["rerank_info"]] == ["A", "B", "A", "B", "A", "B"]
    assert [item["rank"] for item in sent["rerank_info"]] == list(range(1, 7))
    assert sent["monitoring_metrics"]["comparison"]["status"] == "complete"
    assert sent["monitoring_metrics"]["comparison"]["execution_mode"] == "send"
    assert sent["search_filters"]["target_names"] == ["A", "B"]
    assert sent["vector_outcome"] == "complete"
    assert sent["vector_retryable"] is False
    assert "comparison_plan" not in sent
    assert "branch_results" not in sent
    assert calls["synthesis"] == 2


def test_fan_in_calls_global_reranker_and_synthesis_once(monkeypatch):
    calls = _install_fakes(monkeypatch)
    rerank_calls = []

    class Ranker:
        def rerank(self, query, passages, top_n):
            rerank_calls.append((query, len(passages), top_n))
            return passages

    monkeypatch.setattr(comparison, "USE_RERANKER", True)
    monkeypatch.setattr(comparison, "get_ranker", lambda: Ranker())
    result = comparison.build_company_comparison_subgraph().invoke(_state())
    assert rerank_calls == [("A B 비교", 6, 6)]
    assert calls["synthesis"] == 1
    assert result["monitoring_metrics"]["comparison"]["rerank_calls"] == 1
    assert result["monitoring_metrics"]["comparison"]["synthesis_calls"] == 1
    assert result["monitoring_metrics"]["comparison"]["checkpoint_candidate_count"] == 6
    assert result["monitoring_metrics"]["comparison"]["checkpoint_serialized_bytes"] < 512 * 1024
    assert set(result["monitoring_metrics"]["comparison"]["branch_metrics"]) == {"A", "B"}


def test_final_budget_is_honored_after_global_rerank(monkeypatch):
    _install_fakes(monkeypatch)
    state = _state()
    state["retrieval_plan"]["final_budget"] = 3

    result = comparison.build_company_comparison_subgraph().invoke(state)

    assert len(result["rerank_info"]) == 3
    assert [item["rank"] for item in result["rerank_info"]] == [1, 2, 3]
    assert [item["target_name"] for item in result["rerank_info"]] == ["A", "B", "A"]


def test_process_wide_retrieval_limiter_caps_active_branches(monkeypatch):
    state = _state()
    state["search_filters"]["target_names"] = ["A", "B", "C"]
    state["retrieval_plan"]["target_names"] = ["A", "B", "C"]
    active = 0
    peak = 0
    lock = threading.Lock()

    def retrieve(_query, filters):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        target = filters["target_name"]
        return [
            (
                Document(
                    page_content=target,
                    metadata={"target_name": target, "broker": "X"},
                ),
                1.0,
            )
        ], {"snapshot_id": "s", "publication_generation": 2}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    monkeypatch.setattr(
        comparison,
        "_synthesize_answer",
        lambda *_args: ("answer", [AIMessage(content="answer")]),
    )
    original_limit = comparison._retrieval_concurrency_limit
    comparison.configure_retrieval_concurrency(1)
    try:
        result = comparison.build_company_comparison_subgraph().invoke(state)
    finally:
        comparison.configure_retrieval_concurrency(original_limit)

    assert peak == 1
    branch_metrics = result["monitoring_metrics"]["comparison"]["branch_metrics"]
    assert all(item["active_retrievals_at_start"] == 1 for item in branch_metrics.values())
    assert all(item["retrieval_concurrency_limit"] == 1 for item in branch_metrics.values())


def test_send_runs_up_to_target_bounded_concurrency(monkeypatch):
    state = _state()
    targets = ["A", "B", "C", "D", "E"]
    state["search_filters"]["target_names"] = targets
    state["retrieval_plan"]["target_names"] = targets
    active = 0
    peak = 0
    lock = threading.Lock()

    def retrieve(_query, filters):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        target = filters["target_name"]
        return [
            (
                Document(
                    page_content=target,
                    metadata={"target_name": target, "broker": "X"},
                ),
                1.0,
            )
        ], {"snapshot_id": "s", "publication_generation": 2}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    monkeypatch.setattr(
        comparison,
        "_synthesize_answer",
        lambda *_args: ("answer", [AIMessage(content="answer")]),
    )
    original_limit = comparison._retrieval_concurrency_limit
    comparison.configure_retrieval_concurrency(3)
    try:
        result = comparison.build_company_comparison_subgraph().invoke(state)
    finally:
        comparison.configure_retrieval_concurrency(original_limit)

    metrics = result["monitoring_metrics"]["comparison"]
    assert peak == 3
    assert metrics["retrieval_concurrency_limit"] == 3
    assert metrics["observed_peak_retrieval_concurrency"] == 3
    assert metrics["retrieval_wall_ns"] > 0


def test_timed_out_job_keeps_process_retrieval_slot_until_worker_finishes(
    monkeypatch,
):
    active = 0
    peak = 0
    lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def retrieve(_query, filters):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        entered.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        target = filters["target_name"]
        return [
            (
                Document(
                    page_content=target,
                    metadata={"target_name": target, "broker": "X"},
                ),
                1.0,
            )
        ], {"snapshot_id": "s", "publication_generation": 2}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    monkeypatch.setattr(
        comparison,
        "_synthesize_answer",
        lambda *_args: ("answer", [AIMessage(content="answer")]),
    )
    original_limit = comparison._retrieval_concurrency_limit
    comparison.configure_retrieval_concurrency(1)
    first_state = _state()
    second_state = _state()
    second_state["retrieval_plan"]["comparison_id"] = "cmp-2"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                comparison.build_company_comparison_subgraph().invoke,
                first_state,
            )
            assert entered.wait(timeout=1)
            with pytest.raises(FutureTimeoutError):
                first.result(timeout=0.01)
            second = pool.submit(
                comparison.build_company_comparison_subgraph().invoke,
                second_state,
            )
            time.sleep(0.03)
            assert peak == 1
            release.set()
            assert first.result(timeout=2)["vector_outcome"] == "complete"
            assert second.result(timeout=2)["vector_outcome"] == "complete"
    finally:
        release.set()
        comparison.configure_retrieval_concurrency(original_limit)
    assert peak == 1


def test_fan_in_is_deterministic_for_shuffled_branch_completion(monkeypatch):
    _install_fakes(monkeypatch)
    plan = comparison.build_comparison_plan(_state())
    branch_results = {}
    for branch in comparison._branch_inputs(plan):
        branch_results.update(comparison.retrieve_company(branch)["branch_results"])
    items = list(branch_results.items())
    expected = None
    randomizer = Random(7)
    for _ in range(5):
        randomizer.shuffle(items)
        result = comparison.comparison_fan_in(
            {
                **_state(),
                "comparison_plan": plan,
                "branch_results": dict(items),
            }
        )
        normalized = (
            result["generation"],
            result["rerank_info"],
            result["vector_outcome"],
            result["monitoring_metrics"]["comparison"]["citation_ranks_by_target"],
        )
        expected = normalized if expected is None else expected
        assert normalized == expected


def test_crash_resume_reuses_successful_send_branch_writes(monkeypatch):
    retrieval_calls = {"A": 0, "B": 0}
    fan_in_calls = 0

    def retrieve(_query, filters):
        target = filters["target_name"]
        retrieval_calls[target] += 1
        return [
            (
                Document(
                    page_content=target,
                    metadata={"target_name": target, "broker": "X"},
                ),
                1.0,
            )
        ], {"snapshot_id": "s", "publication_generation": 2}

    original_fan_in = comparison.comparison_fan_in

    def fail_once(state):
        nonlocal fan_in_calls
        fan_in_calls += 1
        if fan_in_calls == 1:
            raise RuntimeError("simulated fan-in crash")
        return original_fan_in(state)

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", retrieve)
    monkeypatch.setattr(comparison, "comparison_fan_in", fail_once)
    monkeypatch.setattr(comparison, "USE_RERANKER", False)
    monkeypatch.setattr(
        comparison,
        "_synthesize_answer",
        lambda *_args: ("answer", [AIMessage(content="answer")]),
    )
    graph = comparison.build_company_comparison_subgraph(
        checkpointer=MemorySaver()
    )
    config = {"configurable": {"thread_id": "comparison-crash"}}

    with pytest.raises(RuntimeError, match="simulated fan-in crash"):
        graph.invoke(_state(), config=config)
    result = graph.invoke(None, config=config)

    assert result["vector_outcome"] == "complete"
    assert retrieval_calls == {"A": 1, "B": 1}
    assert fan_in_calls == 2


def test_reranker_failure_falls_back_without_losing_comparison(monkeypatch):
    calls = _install_fakes(monkeypatch)

    class BrokenRanker:
        def rerank(self, query, passages, top_n):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(comparison, "USE_RERANKER", True)
    monkeypatch.setattr(comparison, "get_ranker", lambda: BrokenRanker())

    result = comparison.build_company_comparison_subgraph().invoke(_state())

    assert result["vector_outcome"] == "complete"
    assert result["rerank_info"]
    assert result["monitoring_metrics"]["comparison"]["rerank_degraded"] is True
    assert calls["synthesis"] == 1


def test_partial_is_explicit_and_mixed_revision_never_synthesizes(monkeypatch):
    calls = _install_fakes(monkeypatch)
    state = _state()
    state["question"] = "A와 B와 C를 비교해줘"
    state["rewritten_query"] = "A B C 비교"
    state["search_filters"]["target_names"] = ["A", "B", "C"]
    state["retrieval_plan"]["target_names"] = ["A", "B", "C"]

    def partial(query, filters):
        if filters["target_name"] == "C":
            return [], {"snapshot_id": "s", "publication_generation": 2}
        target = filters["target_name"]
        return [(Document(page_content=target, metadata={"target_name": target, "chunk_uid": target.lower(), "broker": "X"}), 1)], {"snapshot_id": "s", "publication_generation": 2}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", partial)
    result = comparison.build_company_comparison_subgraph().invoke(state)
    assert result["monitoring_metrics"]["comparison"]["status"] == "partial"
    assert result["monitoring_metrics"]["comparison"]["missing_targets"] == ["C"]
    assert result["monitoring_metrics"]["comparison"]["missing_target_statuses"] == {
        "C": "no_result"
    }
    assert result["vector_outcome"] == "partial"
    assert result["vector_retryable"] is False

    def mixed(query, filters):
        generation = 2 if filters["target_name"] == "A" else 3
        target = filters["target_name"]
        return [(Document(page_content=target, metadata={"target_name": target, "broker": "X"}), 1)], {"snapshot_id": "s", "publication_generation": generation}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", mixed)
    before = calls["synthesis"]
    mismatch = comparison.build_company_comparison_subgraph().invoke(state)
    assert mismatch["monitoring_metrics"]["comparison"]["status"] == "revision_mismatch"
    assert mismatch["rerank_info"] == []
    assert mismatch["vector_outcome"] == "revision_mismatch"
    assert mismatch["vector_retryable"] is True
    assert calls["synthesis"] == before


def test_all_failed_is_retryable_but_insufficient_is_not(monkeypatch):
    _install_fakes(monkeypatch)
    monkeypatch.setattr(
        comparison.vectordb,
        "_retrieve_docs_with_scores",
        lambda *_: (_ for _ in ()).throw(
            comparison.vectordb.RetrievalDispatchError("down")
        ),
    )
    failed = comparison.build_company_comparison_subgraph().invoke(_state())
    assert failed["vector_outcome"] == "all_failed"
    assert failed["vector_retryable"] is True

    monkeypatch.setattr(
        comparison.vectordb,
        "_retrieve_docs_with_scores",
        lambda *_: ([], {"snapshot_id": "s", "publication_generation": 2}),
    )
    empty = comparison.build_company_comparison_subgraph().invoke(_state())
    assert empty["vector_outcome"] == "insufficient"
    assert empty["vector_retryable"] is False


def test_one_success_is_insufficient_without_synthesis(monkeypatch):
    calls = _install_fakes(monkeypatch)

    def one_success(_query, filters):
        target = filters["target_name"]
        docs = (
            [
                (
                    Document(
                        page_content="A",
                        metadata={"target_name": "A", "broker": "X"},
                    ),
                    1.0,
                )
            ]
            if target == "A"
            else []
        )
        return docs, {"snapshot_id": "s", "publication_generation": 2}

    monkeypatch.setattr(comparison.vectordb, "_retrieve_docs_with_scores", one_success)
    result = comparison.build_company_comparison_subgraph().invoke(_state())

    assert result["vector_outcome"] == "insufficient"
    assert result["vector_retryable"] is False
    assert result["rerank_info"][0]["target_name"] == "A"
    assert calls["synthesis"] == 0


def test_synthesis_binds_stock_tool_and_defers_generation_on_tool_call(monkeypatch):
    original_synthesize = comparison._synthesize_answer
    _install_fakes(monkeypatch)
    bound_tools = []

    class ToolCallingModel:
        def bind_tools(self, tools):
            bound_tools.extend(tools)
            return self

        def invoke(self, messages):
            assert isinstance(messages[0], HumanMessage)
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_stock_price",
                        "args": {"company_name": "A"},
                        "id": "call-1",
                    }
                ],
            )

    monkeypatch.setattr(comparison, "_synthesize_answer", original_synthesize)
    monkeypatch.setattr(comparison, "build_chat_model", lambda **_: ToolCallingModel())

    result = comparison.build_company_comparison_subgraph().invoke(_state())

    assert bound_tools == comparison.stock_price_tools
    assert result.get("generation") is None
    assert result["messages"][-1].tool_calls[0]["name"] == "get_stock_price"
    assert result["monitoring_metrics"]["comparison"]["synthesis_calls"] == 1


def test_synthesis_prompt_uses_authoritative_publication_metadata(monkeypatch):
    captured = []

    class CapturingModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, messages):
            captured.append(messages[0].content)
            return AIMessage(content="A summary [1]")

    monkeypatch.setattr(comparison, "build_chat_model", lambda **_: CapturingModel())
    candidate = {
        "stable_id": "chunk:a",
        "retrieval_rank": 0,
        "target_name": "A",
        "text": "target-price change date 2026-05-21",
        "score": 0.1,
        "meta": {
            "target_name": "A",
            "report_date": "2026-08-10",
            "broker": "Broker",
            "title": "Latest report",
            "file_name": "a.pdf",
        },
    }

    annotated, contract = comparison.annotate_document_citation_sources(
        [
            {
                "rank": 1,
                "target_name": "A",
                "report_date": "2026-08-10",
                "title": "Latest report",
                "broker": "Broker",
                "report_type": "company",
                "file_name": "a.pdf",
            }
        ]
    )

    answer, _messages, generation_metrics = comparison._synthesize_answer(
        "A latest report",
        "A latest report",
        [candidate],
        [],
        annotated,
        contract,
    )

    assert answer == "A summary [1]"
    assert generation_metrics["call_count"] == 1
    assert "발간일: 2026-08-10" in captured[0]
    assert "목표주가 제시일자를 발간일로 해석하지 마세요" in captured[0]


def test_synthesis_normalizes_passage_citations_to_documents(monkeypatch):
    captured = []

    class CapturingModel:
        def bind_tools(self, _tools):
            return self

        def invoke(self, messages):
            captured.append(messages[0].content)
            return AIMessage(content="요약 [1] [2] [3]")

    monkeypatch.setattr(comparison, "build_chat_model", lambda **_: CapturingModel())

    def _candidate(stable_id: str, file_name: str) -> dict:
        return {
            "stable_id": stable_id,
            "retrieval_rank": 0,
            "target_name": "A",
            "text": f"{file_name} text",
            "score": 0.1,
            "meta": {
                "target_name": "A",
                "report_date": "2026-08-10",
                "broker": "Broker",
                "title": "report",
                "file_name": file_name,
            },
        }

    candidates = [
        _candidate("chunk:1", "a.pdf"),
        _candidate("chunk:2", "a.pdf"),
        _candidate("chunk:3", "b.pdf"),
    ]
    sources = [
        {"rank": index, "target_name": "A", "file_name": candidate["meta"]["file_name"]}
        for index, candidate in enumerate(candidates, 1)
    ]
    annotated, contract = comparison.annotate_document_citation_sources(sources)

    answer, _messages, _metrics = comparison._synthesize_answer(
        "A report", "A report", candidates, [], annotated, contract
    )

    # Passage citations [1][2][3] collapse to document citations [1][2]
    # (chunks 1 and 2 share a.pdf, chunk 3 is b.pdf).
    assert answer == "요약 [1] [2]"
    assert "--- 문서 1 ---" in captured[0]
    assert "--- 문서 2 ---" in captured[0]
    assert "비인용 근거 조각 P" in captured[0]
