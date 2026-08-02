"""Chat messages, citations, issue reporting, and input rendering."""

import os
import subprocess
import sys
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from apps.gui import chat_jobs
from apps.gui import search_engine
from src.configs import config as config_module
from src.core import conversation_store
from src.core import issue_report_store
from src.core.chat_ui_helpers import (
    build_clipboard_copy_html,
    build_no_result_suggestions,
)
from src.utils import citations


def _search_engine_status_content(status: dict) -> tuple[str, str]:
    state = status.get("state")
    if state == "ready":
        return "caption", "검색 엔진 준비 완료"
    if state == "failed":
        return (
            "error",
            "검색 엔진을 준비하지 못했습니다. 다시 준비한 뒤 질문을 처리할 수 있습니다.",
        )
    return (
        "info",
        "검색 엔진을 준비하고 있습니다. 첫 질문은 한 건까지 바로 입력할 수 있으며, "
        "준비가 끝나면 자동으로 처리됩니다.",
    )


@st.fragment(run_every=1.0)
def render_search_engine_status() -> None:
    status = search_engine.start_search_engine_warmup()
    queue_was_pending = bool(
        st.session_state.get("search_engine_queue_was_pending", False)
    )
    if queue_was_pending and not chat_jobs.has_pending_search_engine_job():
        st.session_state["search_engine_queue_was_pending"] = False
        st.rerun(scope="app")
    message_kind, message = _search_engine_status_content(status)
    getattr(st, message_kind)(message)
    if status["state"] == "failed" and st.button(
        "검색 엔진 다시 준비",
        key="retry_search_engine_warmup",
        use_container_width=True,
    ):
        search_engine.retry_search_engine_warmup()
        st.rerun(scope="fragment")


def _render_issue_report_control(
    *,
    current_thread: dict,
    messages: list[dict],
) -> None:
    if report_result := st.session_state.pop("issue_report_success", None):
        st.success("신고 완료. 저장된 파일 경로를 아래에 표시합니다.")
        st.code(report_result["file_path"], language="text")
        try:
            report_text = Path(report_result["file_path"]).read_text(encoding="utf-8")
        except OSError:
            report_text = ""
        if report_text:
            st.caption("신고 텍스트를 복사해 저장/공유하세요.")
            components.html(
                build_clipboard_copy_html(report_text, button_label="클립보드 복사"),
                height=48,
            )
        st.toast(f"신고가 저장되었습니다. (#{report_result['id']})", icon="✅")

    with st.container(key="issue_report_control"):
        _, report_col = st.columns(
            [0.88, 0.12],
            gap="small",
            vertical_alignment="center",
        )
        if report_col.button(
            "신고",
            key=f"toggle_issue_report_{current_thread['id']}",
            help="채팅 화면에서 문제를 빠르게 보고할 수 있습니다.",
            use_container_width=True,
        ):
            st.session_state.show_issue_report_form = not st.session_state.get(
                "show_issue_report_form",
                False,
            )

        if not st.session_state.get("show_issue_report_form"):
            return

        assistant_messages = [
            message
            for message in messages
            if message.get("role") == "assistant"
            and message.get("id") is not None
        ]
        target_label = st.selectbox(
            "문제가 발생한 대상",
            (
                ["특정 응답", "화면·시스템(응답 없음)"]
                if assistant_messages
                else ["화면·시스템(응답 없음)"]
            ),
            key=f"issue_report_response_mode_{current_thread['id']}",
        )
        report_target_type = (
            "response"
            if target_label == "특정 응답"
            else "ui_or_system"
        )
        selected_response_id: str | None = None
        response_by_id = {
            str(message["id"]): message
            for message in assistant_messages
        }
        if report_target_type == "response":
            selected_response_id = st.selectbox(
                "문제가 있는 응답",
                options=list(reversed(response_by_id)),
                format_func=lambda message_id: (
                    f"{message_id}: "
                    f"{str(response_by_id[message_id].get('content') or '')[:48]}"
                ),
                key=f"issue_report_selected_response_{current_thread['id']}",
            )

        category = st.selectbox(
            "신고 분류",
            [
                "일반 답변 품질",
                "검색 정확도 이슈",
                "오답/오류",
                "속도",
                "버그/기능",
                "기타",
            ],
            key=f"issue_report_category_{current_thread['id']}",
        )
        description = st.text_area(
            "추가 설명(선택)",
            placeholder="예: 다른 종목 자료가 섞였어요. / 버튼이 눌리지 않아요.",
            height=110,
            key=f"issue_report_description_{current_thread['id']}",
        )
        include_conversation = st.checkbox(
            "전체 대화 첨부",
            value=False,
            help="기본값은 미첨부입니다. 아래 미리보기를 확인한 뒤 필요한 경우에만 선택하세요.",
            key=f"issue_report_include_context_{current_thread['id']}",
        )
        report_context = (
            issue_report_store.build_issue_report_submission_context(
                thread=current_thread,
                messages=messages,
                report_target_type=report_target_type,
                selected_message_id=selected_response_id,
                include_conversation=include_conversation,
            )
        )
        preview = issue_report_store.build_issue_report_preview(
            context=report_context,
            include_conversation=include_conversation,
        )
        with st.expander("제출 내용 미리보기", expanded=True):
            st.json(preview)
            if include_conversation:
                st.warning(
                    "전체 대화 원문과 메시지 메타데이터가 포함됩니다."
                )
        submitted = st.button(
            "신고 제출",
            key=f"issue_report_submit_{current_thread['id']}",
            use_container_width=True,
        )

        if submitted:
            selected_response = response_by_id.get(
                str(selected_response_id)
            )
            selected_metadata = (
                (selected_response or {}).get("metadata") or {}
            )
            report_kind = (
                "system_error"
                if report_target_type == "ui_or_system"
                or selected_metadata.get("status")
                in {"failed", "error"}
                else "user_feedback"
            )
            report_result = issue_report_store.create_issue_report(
                current_thread["id"],
                category,
                description.strip() or f"{category} 신고",
                report_context,
                kind=report_kind,
                report_target_type=report_target_type,
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


@st.fragment
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
    has_pending_engine_job = chat_jobs.has_pending_search_engine_job()
    st.session_state["search_engine_queue_was_pending"] = has_pending_engine_job
    chat_input_locked = has_running_job or has_pending_engine_job
    if has_running_job:
        waiting_for_engine = any(
            message.get("role") == "assistant"
            and (message.get("metadata") or {}).get("status") == "running"
            and (message.get("metadata") or {}).get("phase") == "waiting_for_engine"
            for message in messages
        )
        if waiting_for_engine:
            st.caption(
                "첫 질문이 대기 중입니다. 검색 엔진 준비가 끝나면 자동으로 처리되며, "
                "그동안 추가 질문 입력은 잠시 잠깁니다."
            )
        else:
            st.caption("이 대화의 답변을 백그라운드에서 생성 중입니다. 다른 대화로 이동해도 작업은 계속됩니다.")
    elif has_pending_engine_job:
        st.caption(
            "다른 대화의 첫 질문이 검색 엔진 준비를 기다리고 있습니다. "
            "준비가 끝나면 여기에서도 질문할 수 있습니다."
        )

    render_search_engine_status()

    with st.container(key="chat_entry_area"):
        user_query = st.chat_input(
            "질문을 입력해 주세요... (ex: 최근 발행된 현대차 리포트 요약해줘)",
            disabled=chat_input_locked,
        )

        _render_issue_report_control(
            current_thread=current_thread,
            messages=messages,
        )

    suggested_query = st.session_state.pop("pending_suggested_query", None)
    if suggested_query and not chat_input_locked:
        user_query = suggested_query

    if user_query:
        should_rename_thread = not messages and (
            current_thread["name"] == "새로운 대화"
            or current_thread["name"].startswith("대화 ")
        )
        if should_rename_thread:
            thread_name = user_query[:15] + "..."
        else:
            thread_name = current_thread["name"]

        prior_search_scope = chat_jobs.latest_search_scope(messages)
        try:
            assistant_message_id = chat_jobs.start_chat_response_job(
                thread_id=current_id,
                thread_name=thread_name,
                user_query=user_query,
                prior_search_scope=prior_search_scope,
            )
        except chat_jobs.PendingSearchEngineQuestionError:
            st.warning(
                "검색 엔진 준비를 기다리는 첫 질문이 이미 있습니다. "
                "준비가 끝난 뒤 다시 입력해 주세요."
            )
            st.rerun(scope="app")
            return
        if should_rename_thread:
            conversation_store.rename_thread(current_id, thread_name)

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
            st.info("질문을 접수했습니다. 검색 엔진 준비 후 자동으로 분석합니다...")
        st.session_state.pending_scroll_anchor = live_anchor_id
        _scroll_to_anchor(live_anchor_id)
        st.rerun(scope="app")
