import re

from src.configs.config import get_logger
from src.configs.prompts import HISTORY_USAGE_DECISION_PROMPT, QUERY_REWRITE_PROMPT
from src.core.followup_scope import is_section_deep_dive_followup, parse_ordinal_reference
from src.core.metadata_filters import resolve_temporal_context
from src.graphs.state import State
from src.llms.factory import build_chat_model

logger = get_logger(__name__)

RECENT_HISTORY_TURNS = 3

GENERIC_SEARCH_TERMS = (
    "발간된",
    "발간",
    "발표된",
    "발표",
    "리포트",
    "보고서",
    "주요",
    "내용",
    "정리",
    "요약",
    "목록",
    "리스트",
    "개수",
    "건수",
    "날짜",
    "을",
    "를",
    "은",
    "는",
    "도",
    "줘",
    "관련",
    "관련된",
    "대한",
    "대해",
    "만",
    "것",
    "것만",
    "중",
    "중에서",
    "에",
    "에서",
    "해서",
    "해",
    "알려줘",
    "알려주세요",
    "해줘",
    "해주세요",
)

FOLLOWUP_SCOPE_MARKERS = (
    "가장 많이",
    "최다",
    "전체 기간",
    "전체기간",
    "기업분석",
    "산업분석",
    "경제분석",
    "위에서",
    "위에",
    "앞서",
    "앞에서",
    "해당 리포트",
    "위 리포트",
    "해당 기간",
    "위 기간",
    "그 기간",
    "주요 내용",
    "위 내용",
    "이 내용",
    "해당 내용",
    "방금",
)

SUMMARY_FOLLOWUP_TERMS = (
    "정리",
    "요약",
    "핵심",
    "투자 포인트",
)

DEICTIC_SCOPE_MARKERS = (
    "위에서",
    "위에",
    "위 내용",
    "이 내용",
    "해당 내용",
    "방금",
    "앞에서",
    "앞서",
    "해당 리포트",
    "위 리포트",
    "해당 기간",
    "위 기간",
    "그 기간",
)

REPORT_TYPE_ONLY_SCOPE_MARKERS = (
    "기업분석",
    "산업분석",
    "경제분석",
)


def _strip_temporal_phrases(text: str) -> str:
    stripped = str(text or "")
    stripped = re.sub(r"20\d{2}\s*(?:년|[-/.])?\s*(?:1[0-2]|0?[1-9])?\s*월?", "", stripped)
    stripped = re.sub(r"(?:1[0-2]|0?[1-9])\s*(?:[-/.]|월)\s*(?:3[01]|[12]\d|0?[1-9])\s*일?", "", stripped)
    stripped = re.sub(r"(?:1[0-2]|0?[1-9])\s*월", "", stripped)
    stripped = re.sub(r"[()（）]\s*(?:월|화|수|목|금|토|일)\s*(?:요일)?\s*[()（）]", "", stripped)
    stripped = re.sub(r"(?<![가-힣])(?:월|화|수|목|금|토|일)요일(?![가-힣])", "", stripped)
    for phrase in (
        "오늘",
        "내일",
        "어제",
        "그제",
        "모레",
        "이번주",
        "금주",
        "다음주",
        "차주",
        "지난주",
        "전주",
        "이번달",
        "이번월",
        "금월",
        "다음달",
        "익월",
        "지난달",
        "전월",
    ):
        stripped = stripped.replace(phrase, "")
    return stripped


def has_explicit_search_topic(question: str) -> bool:
    """검색 주제가 있는지 반환합니다."""
    remainder = _strip_temporal_phrases(question)
    normalized = re.sub(r"\s+", "", remainder)
    for term in sorted(GENERIC_SEARCH_TERMS, key=len, reverse=True):
        normalized = normalized.replace(term, "")
    normalized = re.sub(r"[^\w가-힣]+", "", normalized)
    return len(normalized) >= 2


def is_scope_followup(question: str) -> bool:
    """후속 질문이 이전 범위를 가리키는지 반환합니다."""
    normalized = re.sub(r"\s+", "", str(question or ""))
    if parse_ordinal_reference(normalized) is not None:
        return True
    if any(re.sub(r"\s+", "", keyword) in normalized for keyword in DEICTIC_SCOPE_MARKERS):
        return True
    if is_section_deep_dive_followup(question):
        return True
    if any(normalized == re.sub(r"\s+", "", keyword) for keyword in REPORT_TYPE_ONLY_SCOPE_MARKERS):
        return True
    return (
        any(re.sub(r"\s+", "", keyword) in normalized for keyword in SUMMARY_FOLLOWUP_TERMS)
        and not has_explicit_search_topic(question)
    )


def is_temporal_filter_followup(question: str) -> bool:
    """날짜만 있는 후속 질문인지 반환합니다."""
    return resolve_temporal_context(question) is not None and not has_explicit_search_topic(question)


def is_temporal_broad_search_with_topic(question: str) -> bool:
    """날짜 조건과 독립 주제가 함께 있는지 반환합니다."""
    return resolve_temporal_context(question) is not None and has_explicit_search_topic(question)


def format_recent_history(history: list[tuple[str, str]], *, limit: int = RECENT_HISTORY_TURNS) -> str:
    """최근 history를 rewrite용 문자열로 포맷합니다."""
    lines: list[str] = []
    for role, msg in history[-limit:]:
        content = str(msg or "").strip()
        if not content:
            continue
        lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)


def _parse_history_decision(raw_decision: str) -> bool:
    normalized = str(raw_decision or "").strip().lower()
    if not normalized:
        return False
    first_token = re.split(r"[\s,.:;]+", normalized, maxsplit=1)[0]
    return first_token in {"true", "yes", "y", "1"}


def _build_text_chain(prompt_template: str):
    """Build the LangChain text pipeline only when an LLM call is required."""

    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import PromptTemplate

    llm = build_chat_model(temperature=0.0)
    prompt = PromptTemplate.from_template(prompt_template)
    return prompt | llm | StrOutputParser()


def _llm_history_decision(question: str, history_text: str) -> bool:
    """질문이 최근 history에 의존하는지 LLM으로 분류합니다."""
    if not history_text.strip():
        return False

    chain = _build_text_chain(HISTORY_USAGE_DECISION_PROMPT)
    decision = chain.invoke({"chat_history": history_text, "question": question})
    return _parse_history_decision(decision)


def should_rewrite_with_history(question: str, history: list[tuple[str, str]] | None = None) -> bool:
    """현재 질문이 대화 context를 사용해야 하는지 반환합니다."""
    if not history:
        return False

    if is_temporal_filter_followup(question):
        return True

    if is_scope_followup(question):
        return True

    if is_temporal_broad_search_with_topic(question):
        return False

    history_text = format_recent_history(history)
    try:
        return _llm_history_decision(question, history_text)
    except Exception as exc:  # pragma: no cover - defensive fallback for runtime LLM errors
        logger.warning("[AI] 대화 맥락 사용 여부 판단에 실패했습니다: %s", exc)
        return False


def query_rewrite_node(state: State) -> dict:
    history = state.get("chat_history", [])
    question = state["question"]
    followup_scope_intent = is_scope_followup(question) or is_temporal_filter_followup(question)

    if not history:
        return {
            "rewritten_query": question,
            "uses_chat_history": False,
            "followup_scope_intent": followup_scope_intent,
        }

    uses_chat_history = should_rewrite_with_history(question, history)
    if not uses_chat_history:
        logger.info("[AI] 📝 현재 질문이 독립적인 새 검색으로 판단되어 이전 대화 맥락을 검색어에 주입하지 않습니다.")
        return {
            "rewritten_query": question,
            "uses_chat_history": False,
            "followup_scope_intent": followup_scope_intent,
        }

    history_text = format_recent_history(history)
    chain = _build_text_chain(QUERY_REWRITE_PROMPT)

    rewritten = chain.invoke({"chat_history": history_text, "question": question}).strip()

    if rewritten != question and rewritten:
        logger.info("[AI] 📝 사용자 의도 재분석 완료 (검색어 변경): '%s'", rewritten)
    else:
        rewritten = question

    return {
        "rewritten_query": rewritten,
        "uses_chat_history": True,
        "followup_scope_intent": followup_scope_intent,
    }
