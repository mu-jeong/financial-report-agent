import copy

import pytest

from src.core.expectation_suggester import (
    ExpectationSuggestionError,
    suggest_minimum_expectation,
)


def _candidate() -> dict:
    return {
        "id": "candidate_hynix_missing",
        "category": "답변 누락",
        "impact_summary": "삼성전자만 정리되고 SK하이닉스 내용이 빠졌습니다.",
        "observed": {
            "reproduction_input": {
                "question": "7월 삼성전자와 하이닉스 리포트를 알려줘",
            },
            "actual": {
                "route": "vectordb",
                "filters": {"target_name": "삼성전자"},
                "sources": [
                    {
                        "rank": 1,
                        "target_name": "삼성전자",
                        "file_name": "samsung.pdf",
                    }
                ],
            },
        },
    }


def _source_report() -> dict:
    return {
        "comment": "질문에 포함된 하이닉스 리포트가 답변에서 누락됨",
        "observed": {
            "user_question": "7월 삼성전자와 하이닉스 리포트를 알려줘",
            "assistant_response_preview": (
                "삼성전자 리포트 11건을 정리했습니다. "
                "SK하이닉스 리포트는 조회 결과에 없습니다."
            ),
        },
    }


def test_llm_suggestion_is_canonicalized_without_persisting_the_candidate():
    candidate = _candidate()
    before = copy.deepcopy(candidate)
    captured: dict[str, str] = {}

    def fake_invoke(prompt: str) -> dict:
        captured["prompt"] = prompt
        return {
            "summary": "누락된 SK하이닉스 범위만 확인합니다.",
            "requirements": [
                {
                    "description": "SK하이닉스 리포트 내용을 근거와 함께 다룬다.",
                    "answer_terms_any": [" SK하이닉스 ", "하이닉스"],
                    "source_terms_any": ["SK하이닉스", "하이닉스"],
                    "require_citation": True,
                }
            ],
        }

    suggestion = suggest_minimum_expectation(
        candidate,
        source_report=_source_report(),
        invoke_fn=fake_invoke,
    )

    assert candidate == before
    assert "현재 답변" in captured["prompt"]
    assert "SK하이닉스 리포트는 조회 결과에 없습니다" in captured["prompt"]
    assert suggestion == {
        "summary": "누락된 SK하이닉스 범위만 확인합니다.",
        "requirements": [
            {
                "id": "answer_requirement_1",
                "description": "SK하이닉스 리포트 내용을 근거와 함께 다룬다.",
                "answer_terms_any": ["SK하이닉스", "하이닉스"],
                "source_terms_any": ["SK하이닉스", "하이닉스"],
                "require_citation": True,
            }
        ],
    }


def test_llm_suggestion_uses_the_selected_turn_preview_without_full_history():
    source_report = {
        "comment": "하이닉스 내용 누락",
        "observed": {"user_question": "하이닉스 리포트를 알려줘"},
        "context": {
            "selected_message": {
                "content_preview": "현재 답변에는 삼성전자만 있습니다."
            }
        },
    }
    captured: dict[str, str] = {}

    def fake_invoke(prompt: str) -> dict:
        captured["prompt"] = prompt
        return {
            "summary": "하이닉스 범위 확인",
            "requirements": [
                {
                    "description": "하이닉스 내용을 근거와 함께 다룬다.",
                    "answer_terms_any": ["하이닉스"],
                    "source_terms_any": ["하이닉스"],
                    "require_citation": True,
                }
            ],
        }

    suggest_minimum_expectation(
        _candidate(),
        source_report=source_report,
        invoke_fn=fake_invoke,
    )

    assert "현재 답변에는 삼성전자만 있습니다" in captured["prompt"]


def test_llm_suggestion_rejects_an_unverifiable_contract():
    def fake_invoke(_prompt: str) -> dict:
        return {
            "summary": "근거 없는 조건",
            "requirements": [
                {
                    "description": "하이닉스를 언급한다.",
                    "answer_terms_any": ["하이닉스"],
                    "source_terms_any": [],
                    "require_citation": True,
                }
            ],
        }

    with pytest.raises(
        ExpectationSuggestionError,
        match="검증 가능한 최소 조건을 만들지 못했습니다",
    ):
        suggest_minimum_expectation(_candidate(), invoke_fn=fake_invoke)


def test_vector_suggestion_cannot_disable_source_grounding():
    def fake_invoke(_prompt: str) -> dict:
        return {
            "summary": "단순 문자열 조건",
            "requirements": [
                {
                    "description": "하이닉스를 언급한다.",
                    "answer_terms_any": ["하이닉스"],
                    "source_terms_any": [],
                    "require_citation": False,
                }
            ],
        }

    with pytest.raises(
        ExpectationSuggestionError,
        match="검증 가능한 최소 조건을 만들지 못했습니다",
    ):
        suggest_minimum_expectation(_candidate(), invoke_fn=fake_invoke)


def test_rdb_suggestion_can_use_an_answer_only_condition():
    candidate = _candidate()
    candidate["observed"]["actual"] = {
        "route": "rdb",
        "filters": {},
        "sources": [],
    }

    suggestion = suggest_minimum_expectation(
        candidate,
        invoke_fn=lambda _prompt: {
            "summary": "집계 항목만 확인합니다.",
            "requirements": [
                {
                    "description": "영업이익 집계를 답변한다.",
                    "answer_terms_any": ["영업이익"],
                    "source_terms_any": [],
                    "require_citation": False,
                }
            ],
        },
    )

    assert suggestion["requirements"][0]["source_terms_any"] == []
    assert suggestion["requirements"][0]["require_citation"] is False


def test_llm_prompt_context_is_bounded_before_provider_invocation():
    candidate = _candidate()
    candidate["observed"]["reproduction_input"]["chat_history"] = [
        ["assistant", "H" * 20_000]
        for _ in range(20)
    ]
    source_report = _source_report()
    source_report["observed"]["assistant_response_preview"] = (
        "A" * 20_000
    )
    captured: dict[str, str] = {}

    def fake_invoke(prompt: str) -> dict:
        captured["prompt"] = prompt
        return {
            "summary": "범위 제한 확인",
            "requirements": [
                {
                    "description": "하이닉스 내용을 근거와 함께 다룬다.",
                    "answer_terms_any": ["하이닉스"],
                    "source_terms_any": ["하이닉스"],
                    "require_citation": True,
                }
            ],
        }

    suggest_minimum_expectation(
        candidate,
        source_report=source_report,
        invoke_fn=fake_invoke,
    )

    assert len(captured["prompt"]) < 20_000
    assert "[중간 생략]" in captured["prompt"]


def test_llm_suggestion_wraps_provider_failures():
    def failed_invoke(_prompt: str) -> dict:
        raise TimeoutError("provider timeout with internal details")

    with pytest.raises(
        ExpectationSuggestionError,
        match="LLM 조건 제안에 실패했습니다",
    ) as error:
        suggest_minimum_expectation(_candidate(), invoke_fn=failed_invoke)

    assert "internal details" not in str(error.value)
