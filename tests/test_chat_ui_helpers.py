from pathlib import Path

from src.core.chat_ui_helpers import (
    build_clipboard_copy_html,
    build_no_result_suggestions,
    build_scope_notice,
    escape_numeric_tildes_for_markdown,
)


def test_chat_window_no_result_actions_do_not_show_retry_caption():
    gui_source = "\n".join(
        path.read_text(encoding="utf-8-sig")
        for path in sorted(Path("apps/gui").glob("*.py"))
    )

    assert "검색 조건을 바꿔 다시 시도할 수 있습니다." not in gui_source


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
            "question": "2026년 6월 리포트 알려줘",
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

    assert suggestions == [{"label": "날짜 조건 없이 다시 검색", "query": "NAVER 리포트 요약해줘"}]


def test_build_clipboard_copy_html_escapes_text_and_invokes_clipboard_api():
    html = build_clipboard_copy_html("issue <report> & details")

    assert "navigator.clipboard.writeText" in html
    assert "issue <report> & details" not in html
    assert "Copy issue report" in html


def test_escape_numeric_tildes_for_markdown_preserves_financial_range_text():
    text = "5~10위권 고객사 매출 비중이 과거 1%에서 ~23%로 상승"

    assert escape_numeric_tildes_for_markdown(text) == (
        "5\\~10위권 고객사 매출 비중이 과거 1%에서 \\~23%로 상승"
    )


def test_escape_numeric_tildes_for_markdown_leaves_code_and_explicit_strikethrough():
    text = (
        "~~23% 제외~~와 `5~10` 구간\n"
        "```text\n5~10\n```\n"
        "~~~text\n~23%\n~~~\n"
        "본문 5~10, 이미 \\~23%"
    )

    assert escape_numeric_tildes_for_markdown(text) == (
        "~~23% 제외~~와 `5~10` 구간\n"
        "```text\n5~10\n```\n"
        "~~~text\n~23%\n~~~\n"
        "본문 5\\~10, 이미 \\~23%"
    )


def test_chat_view_escapes_numeric_tildes_at_markdown_render_boundary():
    chat_source = Path("apps/gui/chat_views.py").read_text(encoding="utf-8-sig")

    assert "escape_numeric_tildes_for_markdown(linked_content)" in chat_source


def test_build_scope_notice_distinguishes_reused_prior_date_scope_from_new_date_reset():
    notice = build_scope_notice(
        {
            "question": "기업분석",
            "scope_source": "prior_search_scope",
            "temporal_context": {"description": "이번주=2026-06-15~2026-06-21"},
            "search_filters": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-21",
                "report_type": "company",
            },
        }
    )

    assert notice == "직전 답변의 검색 조건을 이어받아 답변합니다."


def test_build_scope_notice_reports_reset_only_when_current_question_has_date():
    notice = build_scope_notice(
        {
            "question": "6/15(월)",
            "scope_source": "prior_search_scope",
            "temporal_context": {"description": "명시 날짜=2026-06-15"},
            "search_filters": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-15",
                "report_type": "industry",
            },
        }
    )

    assert notice == "새 날짜 조건이 있어 검색 범위를 다시 설정했습니다."

