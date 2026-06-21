import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from src.configs.config import get_logger
from src.configs.prompts import HISTORY_USAGE_DECISION_PROMPT, QUERY_REWRITE_PROMPT
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
    "내용",
    "정리",
    "요약",
    "목록",
    "리스트",
    "개수",
    "건수",
    "날짜",
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
    # Unicode escape strings are used here so Korean markers remain stable
    # across Windows consoles/editors with inconsistent source display encodings.
    "\uac00\uc7a5 \ub9ce\uc774",  # "가장 많이" - most frequent
    "\ucd5c\ub2e4",  # "최다" - top/most
    "\uc804\uccb4 \uae30\uac04",  # "전체 기간" - full period
    "\uc804\uccb4\uae30\uac04",  # "전체기간" - full period without spacing
    "\uae30\uc5c5\ubd84\uc11d",  # "기업분석" - company analysis
    "\uc0b0\uc5c5\ubd84\uc11d",  # "산업분석" - industry analysis
    "\uacbd\uc81c\ubd84\uc11d",  # "경제분석" - economy analysis
    "\uc704\uc5d0\uc11c",  # "위에서" - above/from above
    "\uc704\uc5d0",  # "위에" - above
    "\uc55e\uc11c",  # "앞서" - previously
    "\uc55e\uc5d0\uc11c",  # "앞에서" - earlier/from before
    "\ud574\ub2f9 \ub9ac\ud3ec\ud2b8",  # "해당 리포트" - that report
    "\uc704 \ub9ac\ud3ec\ud2b8",  # "위 리포트" - above report
    "주요 내용",
    "위 내용",
    "이 내용",
    "해당 내용",
    "방금",
    "앞에서",
    "앞서",
)

SUMMARY_FOLLOWUP_TERMS = (
    "정리",
    "요약",
    "핵심",
    "투자 포인트",
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
    """Return whether a query has a non-generic topic besides date/filter words."""
    remainder = _strip_temporal_phrases(question)
    normalized = re.sub(r"\s+", "", remainder)
    for term in GENERIC_SEARCH_TERMS:
        normalized = normalized.replace(term, "")
    normalized = re.sub(r"[^\w가-힣]+", "", normalized)
    return len(normalized) >= 2


def is_scope_followup(question: str) -> bool:
    """Return whether the query points back to the prior retrieved answer scope."""
    normalized = re.sub(r"\s+", "", str(question or ""))
    if any(re.sub(r"\s+", "", keyword) in normalized for keyword in FOLLOWUP_SCOPE_MARKERS):
        return True
    return (
        any(re.sub(r"\s+", "", keyword) in normalized for keyword in SUMMARY_FOLLOWUP_TERMS)
        and not has_explicit_search_topic(question)
    )


def is_temporal_filter_followup(question: str) -> bool:
    """Return whether the question gives a date filter but omits the prior topic."""
    return resolve_temporal_context(question) is not None and not has_explicit_search_topic(question)


def is_temporal_broad_search_with_topic(question: str) -> bool:
    """Return whether a date-filtered query already contains a standalone topic."""
    return resolve_temporal_context(question) is not None and has_explicit_search_topic(question)


def format_recent_history(history: list[tuple[str, str]], *, limit: int = RECENT_HISTORY_TURNS) -> str:
    """Format recent non-empty turns for history-dependency decisions and rewrites."""
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


def _llm_history_decision(question: str, history_text: str) -> bool:
    """Use an LLM to classify whether the question depends on recent history."""
    if not history_text.strip():
        return False

    llm = build_chat_model(temperature=0.0)
    prompt = PromptTemplate.from_template(HISTORY_USAGE_DECISION_PROMPT)
    chain = prompt | llm | StrOutputParser()
    decision = chain.invoke({"chat_history": history_text, "question": question})
    return _parse_history_decision(decision)


def should_rewrite_with_history(question: str, history: list[tuple[str, str]] | None = None) -> bool:
    """Return whether the current question should use conversation context.

    The decision is no longer based on a fixed marker list. Cheap deterministic
    guardrails handle date-only follow-ups and clearly date-scoped broad searches;
    the remaining ambiguous cases are classified semantically from recent history.
    """
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
    llm = build_chat_model(temperature=0.0)

    prompt = PromptTemplate.from_template(QUERY_REWRITE_PROMPT)
    chain = prompt | llm | StrOutputParser()

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
