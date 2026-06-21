from src.core.chat_ux import (
    build_clipboard_copy_html,
    build_no_result_suggestions,
    build_scope_notice,
    build_thread_title,
)


def test_build_thread_title_prefers_target_date_and_comparison_intent():
    assert build_thread_title("2026년 6월 9일 NAVER 리포트들을 증권사별로 비교해줘") == "NAVER 6/9 리포트 비교"


def test_build_thread_title_uses_report_type_when_target_is_missing():
    assert build_thread_title("이번주 반도체 산업 리포트 요약해줘") == "반도체 산업 리포트 요약"


def test_build_scope_notice_explains_prior_scope_reuse():
    notice = build_scope_notice(
        {
            "scope_source": "prior_search_scope",
            "search_filters": {"file_names": ["a.pdf"]},
        }
    )

    assert notice == "직전 답변의 참고 문서 범위 안에서 답변합니다."


def test_build_scope_notice_explains_new_date_scope_reset():
    notice = build_scope_notice(
        {
            "scope_source": "prior_search_scope",
            "temporal_context": {"description": "6월=2026-06-01~2026-06-30"},
            "search_filters": {"report_date_start": "2026-06-01", "report_date_end": "2026-06-30"},
        }
    )

    assert notice == "새 날짜 조건이 있어 검색 범위를 다시 설정했습니다."


def test_build_no_result_suggestions_returns_actionable_retry_queries():
    suggestions = build_no_result_suggestions(
        "2026년 6월 NAVER 리포트 요약해줘",
        {"report_date_start": "2026-06-01", "report_date_end": "2026-06-30"},
    )

    assert suggestions[0] == {"label": "날짜 조건 없이 다시 검색", "query": "NAVER 리포트 요약해줘"}
    assert {"label": "기업 리포트만 검색", "query": "2026년 6월 NAVER 리포트 요약해줘 기업 리포트"} in suggestions
    assert {"label": "산업 리포트까지 포함", "query": "2026년 6월 NAVER 리포트 요약해줘 산업 리포트도 포함"} in suggestions


def test_build_clipboard_copy_html_escapes_text_and_invokes_clipboard_api():
    html = build_clipboard_copy_html("issue <report> & details")

    assert "navigator.clipboard.writeText" in html
    assert "issue <report> & details" not in html
    assert "Copy issue report" in html
