"""Chat messages, citations, issue reporting, and input rendering."""

import os
import subprocess
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from apps.gui import chat_jobs
from src.configs import config as config_module
from src.core import conversation_store
from src.core import issue_report_store
from src.core.chat_ui_helpers import (
    build_clipboard_copy_html,
    build_no_result_suggestions,
)
from src.utils import citations


def _render_issue_report_control(
    *,
    current_thread: dict,
    messages: list[dict],
) -> None:
    if report_result := st.session_state.pop("issue_report_success", None):
        st.success(
            "문제 신고 텍스트 파일을 저장했습니다. 아래 내용을 복사해 btr0813@naver.com 으로 보내주세요."
        )
        st.code(report_result["file_path"], language="text")
        try:
            report_text = Path(report_result["file_path"]).read_text(encoding="utf-8")
        except OSError:
            report_text = ""
        if report_text:
            st.caption("이메일 제목은 신고 텍스트의 '사용 안내'에 포함된 제목 예시를 사용하세요.")
            components.html(
                build_clipboard_copy_html(report_text, button_label="신고 내용 복사"),
                height=48,
            )
        st.toast(f"이슈 리포트가 저장되었습니다. (#{report_result['id']})", icon="✅")

    with st.container(key="issue_report_control"):
        _, report_col = st.columns(
            [0.88, 0.12],
            gap="small",
            vertical_alignment="center",
        )
        if report_col.button(
            "⚠ 신고",
            key=f"toggle_issue_report_{current_thread['id']}",
            help="현재 대화에서 발생한 문제를 개발자가 확인할 수 있도록 저장합니다.",
            use_container_width=True,
        ):
            st.session_state.show_issue_report_form = not st.session_state.get(
                "show_issue_report_form",
                False,
            )

        if not st.session_state.get("show_issue_report_form"):
            return

        with st.form(f"issue_report_form_{current_thread['id']}", clear_on_submit=True):
            category = st.selectbox(
                "문제 유형",
                [
                    "답변 품질 문제",
                    "검색/출처 문제",
                    "응답 지연/멈춤",
                    "화면/사용성 문제",
                    "기타",
                ],
                key=f"issue_report_category_{current_thread['id']}",
            )
            description = st.text_area(
                "무슨 문제가 있었나요?",
                placeholder="예: 질문 의도와 다른 리포트를 참고했어요 / 답변이 너무 오래 걸렸어요 / 출처 링크가 열리지 않아요",
                height=110,
                key=f"issue_report_description_{current_thread['id']}",
            )
            include_conversation = st.checkbox(
                "전체 대화 내용을 함께 첨부",
                value=True,
                help="민감한 내용이 있으면 체크를 해제하고 설명만 제출하세요.",
                key=f"issue_report_include_context_{current_thread['id']}",
            )
            submitted = st.form_submit_button(
                "신고 제출",
                use_container_width=True,
            )

        if submitted:
            clean_description = description.strip()
            if not clean_description:
                st.warning("문제 내용을 한 줄 이상 입력해 주세요.")
                return
            report_result = issue_report_store.create_issue_report(
                current_thread["id"],
                category,
                clean_description,
                issue_report_store.build_issue_report_context(
                    thread=current_thread,
                    messages=messages,
                    include_conversation=include_conversation,
                ),
            )
            st.session_state.show_issue_report_form = False
            st.session_state.issue_report_success = report_result
            st.rerun(scope="app")


def _scroll_to_anchor(anchor_id: str, *, offset_px: int = 104) -> None:
    """Scroll the Streamlit app to an anchor after the current render pass."""
    components.html(
        f"""
        <script>
        const anchorId = {anchor_id!r};
        const offsetPx = {offset_px};
        const scrollToAnchor = () => {{
          const root = window.parent.document;
          const target = root.getElementById(anchorId);
          if (target) {{
            const scrollTop = Math.max(
              0,
              target.getBoundingClientRect().top + window.parent.scrollY - offsetPx
            );
            window.parent.scrollTo({{top: scrollTop, behavior: "auto"}});

            const scrollContainer = root.querySelector('[data-testid="stAppViewContainer"]');
            if (scrollContainer) {{
              const containerTop = Math.max(
                0,
                target.getBoundingClientRect().top
                  - scrollContainer.getBoundingClientRect().top
                  + scrollContainer.scrollTop
                  - offsetPx
              );
              scrollContainer.scrollTo({{top: containerTop, behavior: "auto"}});
            }}
          }}
        }};
        [0, 80, 180, 400, 800, 1400, 2200].forEach((delay) => {{
          setTimeout(scrollToAnchor, delay);
        }});
        </script>
        """,
        height=0,
    )


def _resolve_report_pdf(file_name: str) -> Path | None:
    safe_file_name = Path(file_name).name
    pdf_path = Path(config_module.REPORT_PDF_DIR).expanduser() / safe_file_name
    if pdf_path.exists() and pdf_path.is_file():
        return pdf_path
    return None


def _open_report_pdf(file_name: str) -> tuple[bool, str | None]:
    pdf_path = _resolve_report_pdf(file_name)
    if pdf_path is None:
        return (
            False,
            "PDF 파일을 찾을 수 없습니다. 데이터 업데이트를 다시 실행하거나 REPORT_PDF_DIR 설정을 확인해 주세요.",
        )
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(pdf_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(pdf_path)])
        else:
            subprocess.Popen(["xdg-open", str(pdf_path)])
    except OSError as exc:
        return False, f"PDF를 여는 중 오류가 발생했습니다: {exc}"
    return True, None


def _render_sources(
    rerank_info: list[dict] | None,
    *,
    key_prefix: str,
    anchor_prefix: str,
    used_ranks: set[int] | None = None,
    expanded: bool = False,
) -> None:
    if not rerank_info:
        return
    grouped_sources = citations.group_sources_by_document(rerank_info)
    indexed_sources = [
        {**source_group, "display_rank": display_rank}
        for display_rank, source_group in enumerate(grouped_sources, 1)
    ]
    if used_ranks is not None:
        indexed_sources = [
            source_group
            for source_group in indexed_sources
            if source_group["display_rank"] in used_ranks
        ]
        if not indexed_sources:
            return
    with st.expander(f"참고한 문서 ({len(indexed_sources)}개)", expanded=expanded):
        for index, source_group in enumerate(indexed_sources):
            info = source_group["info"]
            display_rank = source_group["display_rank"]
            rank_label = f"[{display_rank}]"
            file_name = info.get("file_name", "-")
            title = info.get("title") or "-"
            display_text = (
                f"{rank_label} {info.get('target_name', '-')} ({info.get('report_date', '-')}) "
                f"- {info.get('broker', '-')} - {title} - {file_name}"
            )
            st.markdown(
                (
                    f"<div id='{citations.source_anchor_id(anchor_prefix, display_rank)}' "
                    "style='scroll-margin-top: 96px; height: 0; visibility: hidden;'></div>"
                ),
                unsafe_allow_html=True,
            )
            text_col, open_col = st.columns(
                [0.86, 0.14],
                gap="small",
                vertical_alignment="center",
            )
            text_col.write(display_text)
            if open_col.button(
                "열기",
                key=f"{key_prefix}_open_pdf_{index}",
                use_container_width=True,
            ):
                opened, error_message = _open_report_pdf(file_name)
                if not opened:
                    st.warning(error_message)


def _render_no_result_actions(message: dict, *, index: int) -> None:
    metadata = message.get("metadata") or {}
    if not metadata.get("no_vector_results"):
        return
    suggestions = build_no_result_suggestions(
        metadata.get("question") or message.get("content", ""),
        metadata.get("search_filters") or {},
    )
    if not suggestions:
        return
    columns = st.columns(len(suggestions), gap="small")
    for suggestion, column in zip(suggestions, columns):
        if column.button(
            suggestion["label"],
            key=f"no_result_suggestion_{message.get('id', index)}_{suggestion['label']}",
            use_container_width=True,
        ):
            st.session_state.pending_suggested_query = suggestion["query"]
            st.rerun(scope="app")


def _render_message(message: dict, *, index: int) -> None:
    message_anchor_id = chat_jobs.chat_message_anchor_id(message.get("id"), index)
    st.markdown(
        (
            f"<div id='{message_anchor_id}' "
            "style='scroll-margin-top: 104px; height: 1px;'></div>"
        ),
        unsafe_allow_html=True,
    )
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            metadata = message.get("metadata") or {}
            status = metadata.get("status")
            if status == "running":
                st.info(message["content"])
                return
            if status == "failed":
                st.error(message["content"])
                return
            if scope_notice := metadata.get("scope_notice"):
                st.caption(scope_notice)
            selected_sources = (
                metadata.get("selected_sources") or metadata.get("rerank_info") or []
            )
            source_count = len(citations.group_sources_by_document(selected_sources))
            display_content = citations.remove_unavailable_citations(
                message["content"],
                source_count=len(selected_sources),
            )
            display_content = citations.normalize_citation_ranks(
                display_content,
                citations.document_rank_aliases(selected_sources),
            )
            display_content = citations.remove_unavailable_citations(
                display_content,
                source_count=source_count,
            )
            used_ranks = citations.extract_citation_ranks(
                display_content,
                source_count=source_count,
            )
            source_filter_ranks = used_ranks or None
            anchor_prefix = f"message_{index}"
            linked_content = citations.link_citations_to_sources(
                display_content,
                anchor_prefix=anchor_prefix,
                source_count=source_count,
            )
            st.markdown(linked_content)
            _render_sources(
                selected_sources,
                key_prefix=f"message_{index}",
                anchor_prefix=anchor_prefix,
                used_ranks=source_filter_ranks,
                expanded=False,
            )
            _render_no_result_actions(message, index=index)
        else:
            st.markdown(message["content"])


def render_chat(current_id: str, current_thread: dict) -> None:
    st.header(current_thread["name"])

    messages = conversation_store.list_messages(current_id)
    for index, message in enumerate(messages):
        _render_message(message, index=index)

    pending_scroll_anchor = st.session_state.pop("pending_scroll_anchor", None)
    if pending_scroll_anchor:
        _scroll_to_anchor(pending_scroll_anchor)

    has_running_job = chat_jobs.thread_has_running_job(messages)
    if has_running_job:
        st.caption("이 대화의 답변을 백그라운드에서 생성 중입니다. 다른 대화로 이동해도 작업은 계속됩니다.")

    with st.container(key="chat_entry_area"):
        user_query = st.chat_input(
            "질문을 입력해 주세요... (ex: 최근 발행된 현대차 리포트 요약해줘)",
            disabled=has_running_job,
        )

        _render_issue_report_control(
            current_thread=current_thread,
            messages=messages,
        )

    suggested_query = st.session_state.pop("pending_suggested_query", None)
    if suggested_query and not has_running_job:
        user_query = suggested_query

    if user_query:
        if not messages and (
            current_thread["name"] == "새로운 대화"
            or current_thread["name"].startswith("대화 ")
        ):
            thread_name = user_query[:15] + "..."
            conversation_store.rename_thread(current_id, thread_name)
        else:
            thread_name = current_thread["name"]

        prior_search_scope = chat_jobs.latest_search_scope(messages)
        conversation_store.append_message(current_id, "user", user_query)
        assistant_message_id = chat_jobs.start_chat_response_job(
            thread_id=current_id,
            thread_name=thread_name,
            user_query=user_query,
            prior_search_scope=prior_search_scope,
        )

        with st.chat_message("user"):
            st.markdown(user_query)

        live_anchor_id = chat_jobs.chat_message_anchor_id(
            assistant_message_id,
            len(messages) + 1,
        )
        st.markdown(
            (
                f"<div id='{live_anchor_id}' "
                "style='scroll-margin-top: 104px; height: 1px;'></div>"
            ),
            unsafe_allow_html=True,
        )
        with st.chat_message("assistant"):
            st.info("AI가 리포트 내용을 검색하고 분석 중입니다...")
        st.session_state.pending_scroll_anchor = live_anchor_id
        _scroll_to_anchor(live_anchor_id)
        st.rerun(scope="app")
