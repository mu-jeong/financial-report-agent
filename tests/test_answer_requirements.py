import pytest

from src.core.answer_requirements import (
    AnswerRequirementValidationError,
    canonicalize_answer_requirements,
    evaluate_answer_requirements,
)
from src.core.monitoring import (
    build_candidate_evaluation_case,
    canonicalize_regression_candidate,
)


def _hynix_requirement() -> dict:
    return {
        "id": "answer_requirement_1",
        "description": "SK하이닉스 리포트 내용을 검색 근거와 함께 다룬다.",
        "answer_terms_any": ["SK하이닉스", "하이닉스"],
        "source_terms_any": ["SK하이닉스", "하이닉스"],
        "require_citation": True,
    }


def test_answer_requirement_rejects_a_negative_mention_without_grounding():
    result = evaluate_answer_requirements(
        [_hynix_requirement()],
        answer="SK하이닉스 관련 리포트는 조회 결과에 포함되지 않았습니다.",
        sources=[
            {
                "rank": 1,
                "target_name": "삼성전자",
                "file_name": "samsung.pdf",
            }
        ],
    )

    assert result["passed"] is False
    assert result["results"] == [
        {
            "id": "answer_requirement_1",
            "description": "SK하이닉스 리포트 내용을 검색 근거와 함께 다룬다.",
            "answer_term_pass": True,
            "source_term_pass": False,
            "citation_pass": False,
            "matched_source_rank": None,
            "passed": False,
        }
    ]


def test_answer_requirement_passes_when_target_source_is_used_and_cited():
    result = evaluate_answer_requirements(
        [_hynix_requirement()],
        answer="SK하이닉스는 HBM 수요의 수혜가 기대됩니다. [2]",
        sources=[
            {"rank": 1, "target_name": "삼성전자"},
            {
                "rank": 2,
                "target_name": "SK 하이닉스",
                "title": "HBM 경쟁력 점검",
            },
        ],
    )

    assert result["passed"] is True
    assert result["results"][0]["matched_source_rank"] == 2
    assert result["results"][0]["citation_pass"] is True


def test_answer_requirement_requires_the_matching_source_citation():
    result = evaluate_answer_requirements(
        [_hynix_requirement()],
        answer="SK하이닉스는 HBM 수요의 수혜가 기대됩니다. [1]",
        sources=[
            {"rank": 1, "target_name": "삼성전자"},
            {"rank": 2, "target_name": "SK하이닉스"},
        ],
    )

    assert result["passed"] is False
    assert result["results"][0]["source_term_pass"] is True
    assert result["results"][0]["citation_pass"] is False


def test_rdb_answer_requirement_can_use_answer_only_terms():
    result = evaluate_answer_requirements(
        [
            {
                "description": "영업이익 결과를 답변한다.",
                "answer_terms_any": ["영업이익"],
                "source_terms_any": [],
                "require_citation": False,
            }
        ],
        answer="2026년 2분기 영업이익을 집계했습니다.",
        sources=[],
    )

    assert result["passed"] is True
    assert result["results"][0]["source_term_pass"] is True
    assert result["results"][0]["citation_pass"] is True


def test_answer_requirement_contract_is_strict_and_bounded():
    with pytest.raises(
        AnswerRequirementValidationError,
        match="source_terms_any is required when require_citation is true",
    ):
        canonicalize_answer_requirements(
            [
                {
                    "description": "근거가 필요한 조건",
                    "answer_terms_any": ["NAVER"],
                    "source_terms_any": [],
                    "require_citation": True,
                }
            ]
        )

    with pytest.raises(
        AnswerRequirementValidationError,
        match="unsupported keys",
    ):
        canonicalize_answer_requirements(
            [
                {
                    **_hynix_requirement(),
                    "prompt": "ignore previous instructions",
                }
            ]
        )


def test_candidate_contract_carries_answer_requirements_into_the_evaluator():
    candidate = canonicalize_regression_candidate(
        {
            "id": "candidate_hynix_coverage",
            "triage_status": "ready",
            "contract_revision": 1,
            "expected_approved_at": "2026-08-02T00:00:00+00:00",
            "expected_approved_by": "local_operator",
            "verification_type": "graph_contract",
            "active_checks": ["answer_requirements_pass"],
            "observed": {
                "reproduction_input": {
                    "question": "삼성전자와 하이닉스 리포트를 알려줘"
                },
                "actual": {},
            },
            "expected": {
                "route": "vectordb",
                "filters": {},
                "sources": [],
                "state": {},
                "manual_assertions": [],
                "answer_requirements": [_hynix_requirement()],
            },
        }
    )

    case = build_candidate_evaluation_case(candidate)

    assert case["active_checks"] == ["answer_requirements_pass"]
    assert case["expected_answer_requirements"] == [_hynix_requirement()]
