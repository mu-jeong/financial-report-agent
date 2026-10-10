"""Streamlit view for company report synthesis over a fixed RDB corpus."""

from __future__ import annotations

import hashlib
import uuid
from calendar import monthrange
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import streamlit as st

from apps.gui import synthesis_jobs
from src.core.chat_ui_helpers import display_report_content, escape_numeric_tildes_for_markdown
from src.configs import config as config_module
from src.core import conversation_store, report_corpus, report_synthesis
from src.utils import citations


_OWNER_KEY = "synthesis_owner_id"
_JOB_KEY = "synthesis_current_job_id"
_PRESET_KEY = "synthesis_period_preset"
_COMPANY_KEY = "synthesis_company"
_DATE_RANGE_KEY = "synthesis_date_range"
_PREVIEW_KEY = "synthesis_confirmed_corpus"
_PREVIEW_ERROR_KEY = "synthesis_preview_error"
_SCOPE_KEY = "synthesis_saved_scope"
_ANCHOR_PREFIX = "synthesis-result"
_OWNER_QUERY_PARAM = "synthesis_session"
_REPORT_BODY_KEY = "synthesis_report_body"
_REPORT_METRICS_KEY = "synthesis_report_metrics"
_REPORT_BODY_STYLE = """
<style>
.st-key-synthesis_report_body {
    max-width: 88ch;
    margin: 0.4rem auto 1.5rem;
}
.st-key-synthesis_report_body [data-testid="stMarkdownContainer"],
.st-key-synthesis_report_body [data-testid="stMarkdownContainer"] p,
.st-key-synthesis_report_body [data-testid="stMarkdownContainer"] li {
    line-height: 1.8;
}
.st-key-synthesis_report_body [data-testid="stMarkdownContainer"] h2,
.st-key-synthesis_report_body [data-testid="stMarkdownContainer"] h3 {
    margin-top: 1.5rem;
    margin-bottom: 0.65em;
}
.st-key-synthesis_report_body [data-testid="stMarkdownContainer"] h3:first-child {
    margin-top: 0;
}
@media (max-width: 640px) {
    .st-key-synthesis_report_metrics [data-testid="stHorizontalBlock"] {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.35rem 0.6rem;
    }
    .st-key-synthesis_report_metrics [data-testid="stColumn"] {
        width: 100% !important;
        min-width: 0 !important;
    }
    .st-key-synthesis_report_metrics [data-testid="stMetric"] {
        padding: 0.25rem 0;
    }
}
</style>
"""


def _session_owner_id() -> str:
    def canonical_uuid(value: Any) -> str | None:
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = str(value or "").strip().lower()
        try:
            parsed = uuid.UUID(text)
        except (ValueError, AttributeError):
            return None
        canonical = str(parsed)
        return canonical if text == canonical else None

    owner_id = canonical_uuid(st.session_state.get(_OWNER_KEY))
    query_params = getattr(st, "query_params", None)
    query_owner = canonical_uuid(query_params.get(_OWNER_QUERY_PARAM)) if query_params is not None else None
    if owner_id is None:
        owner_id = query_owner or str(uuid.uuid4())
        st.session_state[_OWNER_KEY] = owner_id
    if query_params is not None and query_owner != owner_id:
        query_params[_OWNER_QUERY_PARAM] = owner_id
    return owner_id


def _subtract_months(value: date, months: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _preset_range(preset: str, *, today: date | None = None) -> tuple[date, date]:
    end = today or date.today()
    months = 3 if preset == "최근 3개월" else 1
    return _subtract_months(end, months), end


def _normalized_range(value: Any) -> tuple[date | None, date | None]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        start, end = value
        if isinstance(start, date) and isinstance(end, date):
            return start, end
    return None, None


def _restore_scope_widgets(companies: list[str], job: Mapping[str, Any] | None) -> None:
    # Streamlit removes hidden widget keys when navigating away. Keep the last
    # submitted scope independent of widgets; an owned job also survives reloads.
    saved = (st.session_state.get(_SCOPE_KEY) or st.session_state.get(_PREVIEW_KEY) or job)
    if not isinstance(saved, Mapping) or saved.get("company") not in companies:
        return
    if _COMPANY_KEY not in st.session_state:
        st.session_state[_COMPANY_KEY] = saved["company"]
    try:
        start, end = date.fromisoformat(saved["start_date"]), date.fromisoformat(saved["end_date"])
    except (KeyError, TypeError, ValueError):
        return
    if start > end or end > date.today():
        return
    if _DATE_RANGE_KEY not in st.session_state:
        st.session_state[_DATE_RANGE_KEY] = (start, end)
    if _PRESET_KEY not in st.session_state:
        preset = saved.get("preset")
        st.session_state[_PRESET_KEY] = preset if preset in {"최근 1개월", "최근 3개월"} else "직접 선택"


def _document_file_name(document: Mapping[str, Any]) -> str:
    explicit = str(document.get("file_name") or "").strip()
    if explicit:
        return Path(explicit).name
    relative = str(document.get("canonical_relative_path") or "").strip()
    return PurePosixPath(relative).name if relative else "리포트.pdf"


def _document_uid(document: Mapping[str, Any]) -> str:
    return str(
        document.get("report_uid")
        or document.get("document_uid")
        or document.get("uid")
        or ""
    ).strip()


def _corpus_counts(corpus: Mapping[str, Any]) -> dict[str, int]:
    documents = list(corpus.get("documents") or [])
    excluded = list(corpus.get("excluded") or [])
    brokers = {
        str(item.get("broker") or "").strip()
        for item in documents
        if isinstance(item, Mapping) and str(item.get("broker") or "").strip()
    }
    return {
        "total": len(documents) + len(excluded),
        "ready": len(documents),
        "excluded": len(excluded),
        "brokers": len(brokers),
    }


def _workload_error(corpus: Mapping[str, Any]) -> str | None:
    """Use the synthesis service's public preflight before starting a job."""
    try:
        report_synthesis.validate_corpus(dict(corpus))
    except ValueError as exc:
        return str(exc)
    else:
        return None


def _safe_pdf_path(document: Mapping[str, Any]) -> Path | None:
    raw_relative_path = str(document.get("canonical_relative_path") or "").strip()
    if not raw_relative_path:
        return None
    relative = PurePosixPath(raw_relative_path)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        return None
    root = Path(config_module.REPORT_PDF_DIR).expanduser().resolve()
    data_root = Path(config_module.DATA_ROOT).expanduser().resolve()
    candidates = (
        data_root.joinpath(*relative.parts).resolve(),
        root.joinpath(*relative.parts).resolve(),
    )
    for candidate in candidates:
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _verified_pdf(document: Mapping[str, Any]) -> tuple[bytes | None, str]:
    path = _safe_pdf_path(document)
    if path is None:
        return None, "저장된 원문을 찾을 수 없습니다."
    try:
        payload = path.read_bytes()
    except OSError:
        return None, "저장된 원문을 읽을 수 없습니다."
    expected_hash = str(document.get("source_sha256") or "").strip().lower()
    if not expected_hash or hashlib.sha256(payload).hexdigest() != expected_hash:
        return None, "종합 생성 후 원문이 변경되어 열 수 없습니다. 새 종합을 생성해 주세요."
    return payload, ""


def _result_scope(result: Mapping[str, Any]) -> str:
    return (
        f"{result.get('company') or '-'} · "
        f"{result.get('start_date') or '-'} ~ {result.get('end_date') or '-'}"
    )


def _uses_raw_body_pipeline(result: Mapping[str, Any]) -> bool:
    return result.get("processing_mode") == "raw_body"


def _display_report_content(result: Mapping[str, Any]) -> str:
    """Hide only the exact raw-writer wrapper already shown by the view."""
    content = display_report_content(result)
    if not _uses_raw_body_pipeline(result):
        return content
    counts = result.get("counts")
    coverage = result.get("coverage")
    if not isinstance(counts, Mapping) or not isinstance(coverage, Mapping):
        return content
    prefix = (
        f"## {result.get('company')} 기간 내 리포트 종합\n\n"
        f"{result.get('start_date')} ~ {result.get('end_date')} · "
        f"본문 준비 {int(counts.get('completed') or 0)}건 · "
        f"인용 근거 {int(coverage.get('cited') or 0)}건 · "
        f"{int(counts.get('brokers') or 0)}개 증권사\n\n"
    )
    return content[len(prefix):] if content.startswith(prefix) else content


def _result_evidence_by_document(result: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    raw_evidence = result.get("evidence") or []
    items = raw_evidence.values() if isinstance(raw_evidence, Mapping) else raw_evidence
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    seen: dict[str, set[tuple[Any, Any, Any, str]]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        uid = _document_uid(item)
        quote = str(item.get("quote") or "").strip()
        if uid and quote:
            origin = (item.get("parent_uid"), item.get("start"), item.get("end"), quote)
            if origin in seen.setdefault(uid, set()):
                continue
            seen[uid].add(origin)
            grouped.setdefault(uid, []).append(item)
    return grouped


def _source_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = list(result.get("selected_sources") or [])
    contract = result.get("citation_contract")
    validation = citations.validate_citation_contract(sources, contract)
    if validation["status"] == citations.CITATION_CONTRACT_VALID:
        return [
            {**group["info"], "display_rank": group["document_rank"]}
            for group in citations.group_sources_by_persisted_document_rank(sources, contract)
        ]
    return []


def _followup_metadata(result: Mapping[str, Any]) -> dict[str, Any]:
    completed_statuses = {"prepared"} if _uses_raw_body_pipeline(result) else {"completed"}
    documents = [
        item
        for item in result.get("documents") or []
        if isinstance(item, Mapping) and item.get("status") in completed_statuses
    ]
    completed_uids = {_document_uid(item) for item in documents}
    sources = [
        item
        for item in result.get("selected_sources") or []
        if isinstance(item, Mapping) and _document_uid(item) in completed_uids
    ]
    selected_uids = [
        uid
        for uid in (_document_uid(item) for item in sources)
        if uid and uid in completed_uids
    ]
    document_uids = list(dict.fromkeys(selected_uids))
    selected_uid_set = set(document_uids)
    file_names = list(
        dict.fromkeys(
            _document_file_name(item)
            for item in sources
            if _document_uid(item) in selected_uid_set
        )
    )
    filters = {
        "target_name": result.get("company"),
        "report_type": "company",
        "report_date_start": result.get("start_date"),
        "report_date_end": result.get("end_date"),
        "file_names": file_names,
    }
    search_scope = {
        "search_filters": filters,
        "file_names": file_names,
        "document_uids": document_uids,
        "revision": result.get("revision"),
        "fingerprint": result.get("fingerprint"),
        "scope_source": "report_synthesis",
    }
    return {
        "status": "succeeded",
        "route": "rdb",
        "selected_sources": sources,
        "citation_contract": result.get("citation_contract"),
        "search_scope": search_scope,
        "search_filters": filters,
        "document_uids": document_uids,
        "synthesis_fingerprint": result.get("fingerprint"),
    }


def _open_followup_chat(result: Mapping[str, Any]) -> None:
    company = str(result.get("company") or "기업")
    thread_id = conversation_store.create_thread(f"{company} 리포트 종합")
    conversation_store.append_message(
        thread_id,
        "assistant",
        display_report_content(result) or "종합 결과가 없습니다.",
        _followup_metadata(result),
    )
    st.session_state.current_thread_id = thread_id
    st.session_state.active_user_page = "Chat"
    # The monitoring radio was already rendered by the sidebar in this run.
    # Apply its navigation reset before creating widgets on the next app run.
    st.session_state._return_to_chat = True
    st.rerun(scope="app")


def _render_document_list(result: Mapping[str, Any], *, source_changed: bool) -> None:
    documents = [item for item in result.get("documents") or [] if isinstance(item, Mapping)]
    source_rows = _source_rows(result)
    rank_by_uid = {_document_uid(source): int(source["display_rank"]) for source in source_rows}
    document_uids = {_document_uid(document) for document in documents}
    documents.extend(source for source in source_rows if _document_uid(source) not in document_uids)
    if not documents:
        return
    raw_body = _uses_raw_body_pipeline(result)
    evidence_by_document = _result_evidence_by_document(result) if raw_body else {}
    uncited = int((result.get("coverage") or {}).get("uncited") or 0)
    if uncited:
        st.caption(f"대상 리포트 중 {uncited}건은 최종 분석에 반영되지 않았습니다.")
    for index, document in enumerate(documents):
        status = str(document.get("status") or "unknown")
        status_labels = (
            {"prepared": "본문 준비 완료", "failed": "본문 준비 실패", "excluded": "준비 제외"}
            if raw_body
            else {"completed": "분석 완료", "failed": "분석 실패", "excluded": "분석 제외"}
        )
        status_label = status_labels.get(status, status)
        if raw_body and document.get("synthesis_status") == "uncited":
            status_label = "최종 분석 미반영"
        title = str(document.get("title") or _document_file_name(document))
        rank = rank_by_uid.get(_document_uid(document))
        if rank is not None:
            st.markdown(
                f"<div id='{citations.source_anchor_id(_ANCHOR_PREFIX, rank)}'></div>",
                unsafe_allow_html=True,
            )
        label = (
            (f"[{rank}] " if rank is not None else "") +
            f"{document.get('report_date') or '-'} · {document.get('broker') or '-'} · "
            f"{title} · {status_label}"
        )
        with st.expander(label):
            reason = document.get("reason") or document.get("error")
            if reason:
                st.caption(f"사유: {reason}")
            if raw_body:
                preparation = document.get("preparation")
                if isinstance(preparation, Mapping) and status == "prepared":
                    segments = int(preparation.get("total_segments") or 0)
                    body_chars = int(preparation.get("body_chars") or 0)
                    st.caption(f"본문 {body_chars:,}자 · {segments}개 구간 준비")
                document_evidence = evidence_by_document.get(_document_uid(document), [])
                if document_evidence:
                    st.caption("리포트에 사용한 원문 근거")
                for evidence in document_evidence:
                    quote = str(evidence.get("quote") or "").strip()
                    st.markdown(escape_numeric_tildes_for_markdown(f"> {quote}"))
                if status == "prepared" and not document_evidence:
                    st.caption("최종 리포트에서 직접 인용한 원문 근거가 없습니다.")
            analysis = document.get("analysis")
            if not raw_body and isinstance(analysis, Mapping):
                claims = [claim for claim in analysis.get("claims") or [] if isinstance(claim, Mapping)]
                for claim in claims:
                    topic = str(claim.get("topic") or "주요 내용")
                    claim_text = str(claim.get("claim") or "").strip()
                    quote = str(claim.get("quote") or "").strip()
                    if claim_text:
                        st.markdown(escape_numeric_tildes_for_markdown(
                            f"**{topic}** — {claim_text}"
                        ))
                    if quote:
                        st.markdown(escape_numeric_tildes_for_markdown(f"> {quote}"))
                failed_chunks = int(analysis.get("failed_chunks") or 0)
                if failed_chunks:
                    st.caption(f"본문 구간 {failed_chunks}개를 처리하지 못했습니다.")
                    failure_labels = {
                        "timeout": "모델 응답 대기 시간 초과",
                        "evidence_mismatch": "모델의 인용이 원문과 일치하지 않음",
                        "json_format": "모델 응답의 JSON 형식 오류",
                        "output_validation": "모델 응답 형식 검증 실패",
                        "rate_limit": "모델 요청 한도 초과",
                        "connection": "모델 연결 오류",
                        "provider_http": "모델 서비스 요청 오류",
                        "provider_error": "모델 호출 오류",
                    }
                    for failure in analysis.get("errors") or []:
                        if not isinstance(failure, Mapping) or failure.get("category") not in failure_labels:
                            continue
                        label = failure_labels[failure["category"]]
                        elapsed_ms = failure.get("elapsed_ms")
                        if type(elapsed_ms) in {int, float} and elapsed_ms >= 0:
                            label += f" · {elapsed_ms / 1000:.1f}초"
                        st.caption(label)
                if not claims and not failed_chunks and status == "completed":
                    st.caption("본문 분석은 완료했지만 표시할 주요 주장이 없습니다.")
            if source_changed:
                st.caption("자료 구성이 바뀌어 이 결과의 원문 열기를 제한했습니다.")
                continue
            payload, error = _verified_pdf(document)
            if payload is None:
                st.caption(error)
            else:
                st.download_button(
                    "원문 PDF 다운로드",
                    data=payload,
                    file_name=_document_file_name(document),
                    mime="application/pdf",
                    key=f"synthesis_pdf_{result.get('fingerprint')}_{index}",
                )


def _current_fingerprint(result: Mapping[str, Any]) -> str | None:
    try:
        current = report_corpus.load_report_corpus(
            str(result.get("company") or ""),
            str(result.get("start_date") or ""),
            str(result.get("end_date") or ""),
        )
    except (OSError, RuntimeError, ValueError):
        return None
    return str(current.get("fingerprint") or "")


def _render_result(result: Mapping[str, Any], *, current_corpus: Mapping[str, Any] | None = None) -> None:
    st.divider()
    st.subheader("기간 내 기업 흐름 리포트")
    st.caption(escape_numeric_tildes_for_markdown(
        f"적용 범위: {_result_scope(result)} · 생성 시점에 고정한 자료 기준 · "
        "리포트 발행일은 사건 발생일과 다를 수 있습니다."
    ))

    for warning in result.get("warnings") or []:
        st.warning(str(warning))

    stored_prompt_version = str(result.get("prompt_version") or "").strip()
    if stored_prompt_version:
        if stored_prompt_version != report_synthesis.REPORT_PROMPT_VERSION:
            st.warning(
                "이 결과는 이전 작성 방식으로 생성되어 기간 내 기업 흐름 형식을 보장하지 않습니다. "
                "같은 조회 범위에서 새 리포트를 생성해 주세요."
            )

    raw_body = _uses_raw_body_pipeline(result)
    counts = dict(result.get("counts") or {})
    with st.container(key=_REPORT_METRICS_KEY):
        columns = st.columns(5)
        for column, label, key in zip(
            columns,
            (("확인", "본문 준비", "준비 실패", "제외", "증권사") if raw_body
             else ("확인", "분석 완료", "실패", "제외", "증권사")),
            ("total", "completed", "failed", "excluded", "brokers"),
        ):
            unit = "곳" if key == "brokers" else "건"
            column.metric(label, f"{int(counts.get(key) or 0)}{unit}")

    result_status = str(result.get("status") or "completed")
    coverage = result.get("coverage") or {}
    if result_status == "partial" or int(counts.get("failed") or 0) > 0:
        if raw_body:
            if int(counts.get("failed") or 0) > 0:
                st.warning("일부 리포트의 본문을 읽지 못해 준비된 문서만으로 종합했습니다. 아래 문서별 상태를 확인해 주세요.")
            if int(counts.get("excluded") or 0) > 0:
                st.warning("준비 제외 자료는 이 리포트의 판단에 포함되지 않았습니다. 아래 문서별 상태를 확인해 주세요.")
        else:
            st.warning("일부 리포트를 분석하지 못해 완료된 문서만으로 종합했습니다. 아래 문서별 상태를 확인해 주세요.")
    cited_count = coverage.get("cited", counts.get("completed")) if raw_body else counts.get("completed")
    if int(cited_count or 0) == 1:
        label = "검증된 인용 근거가 있는" if raw_body else "분석 완료"
        st.info(f"{label} 리포트가 1건입니다. 이 문서를 근거로 작성했으며 증권사 간 합의를 판단할 수 없습니다.")

    sources = list(result.get("selected_sources") or [])
    source_rows = _source_rows(result)
    if sources and not source_rows:
        st.warning("저장된 인용 정보를 검증할 수 없어 본문 인용 링크와 후속 질문을 제한했습니다.")
    linked_content = citations.link_citations_to_sources(
        _display_report_content(result),
        anchor_prefix=_ANCHOR_PREFIX,
        source_count=len(source_rows),
    )
    st.markdown(_REPORT_BODY_STYLE, unsafe_allow_html=True)
    with st.container(key=_REPORT_BODY_KEY):
        st.markdown(escape_numeric_tildes_for_markdown(linked_content))

    original_fingerprint = str(result.get("fingerprint") or "")
    scope_matches = current_corpus is not None and all(
        current_corpus.get(key) == result.get(key) for key in ("company", "start_date", "end_date")
    )
    current_fingerprint = (str(current_corpus.get("fingerprint") or "") if scope_matches
                           else _current_fingerprint(result))
    source_unverified = not current_fingerprint or not original_fingerprint
    source_changed = source_unverified or current_fingerprint != original_fingerprint
    if source_unverified:
        st.warning("현재 자료 구성을 확인할 수 없어 원문 열기와 후속 질문을 제한했습니다. 데이터 상태를 확인한 뒤 새 결과를 생성해 주세요.")
    elif source_changed:
        st.warning("종합 생성 후 대상 자료가 변경되었습니다. 정확한 원문과 후속 질문 범위를 보장하려면 새 결과를 생성해 주세요.")

    if source_rows or result.get("documents"):
        source_label = "인용한 문서" if raw_body else "분석한 문서"
        with st.expander(f"근거 부록 · {source_label} ({len(source_rows)}개)", expanded=False):
            _render_document_list(result, source_changed=source_changed)
    if st.button(
        "이 범위에서 질문하기",
        key=f"synthesis_followup_{original_fingerprint}",
        disabled=source_changed or not bool(source_rows),
        width="stretch",
    ):
        _open_followup_chat(result)


def _latest_owned_job(owner_id: str) -> dict[str, Any] | None:
    current_id = st.session_state.get(_JOB_KEY)
    if current_id:
        current = synthesis_jobs.get_job(str(current_id), owner_id)
        if current is not None:
            return current
    jobs = synthesis_jobs.list_jobs(owner_id, limit=20)
    if not jobs:
        return None
    running = next((job for job in jobs if job.get("status") == "running"), None)
    selected = running or jobs[0]
    if selected.get("id"):
        st.session_state[_JOB_KEY] = str(selected["id"])
    return selected


def _render_manual_retry(job: Mapping[str, Any], owner_id: str, *, partial: bool) -> None:
    result = job.get("result")
    raw_body = isinstance(result, Mapping) and _uses_raw_body_pipeline(result)
    partial_content = raw_body and (int((result.get("coverage") or {}).get("uncited") or 0) > 0
                                   or bool(result.get("validation_warnings")))
    if raw_body:
        st.caption("같은 고정 자료로 부분 종합을 다시 작성합니다. 완전하게 검증된 종합 묶음은 재사용합니다."
                   if partial_content else
            "자동으로 재시도하지 않습니다. 아래 버튼을 누르면 실패한 종합 단계를 다시 처리하며, "
            "완료된 종합 묶음은 재사용합니다."
        )
    else:
        st.caption("자동으로 재시도하지 않습니다. 아래 버튼을 누르면 실패한 범위를 다시 처리합니다.")
    if partial:
        should_retry = st.button(
            "부분 종합 다시 생성" if partial_content else "실패한 종합 다시 시도" if raw_body else "미완료 문서 다시 시도",
            key=f"retry_partial_synthesis_{job.get('id')}",
        )
    else:
        should_retry = st.button(
            "실패한 작업 다시 시도", key=f"retry_synthesis_{job.get('id')}"
        )
    if not should_retry:
        return
    try:
        new_id = synthesis_jobs.retry_job(str(job["id"]), owner_id)
    except (RuntimeError, ValueError) as exc:
        st.error(str(exc))
    else:
        st.session_state[_JOB_KEY] = new_id
        st.rerun(scope="app")


def _render_job_failure(job: Mapping[str, Any]) -> None:
    result = job.get("result")
    result_error = result.get("error") if isinstance(result, Mapping) else None
    st.error(str(job.get("error") or result_error or "종합 생성에 실패했습니다."))
    diagnostic = result.get("diagnostic") if isinstance(result, Mapping) else None
    if not isinstance(diagnostic, Mapping):
        return
    reasons = {
        "empty_response": "빈 응답: 모델이 최종 JSON 본문을 반환하지 않았습니다.",
        "response_truncated": "출력 중단: 모델 응답이 출력 토큰 한도에 도달했습니다.",
        "response_interrupted": "응답 중단: 모델 서버가 최종 응답을 완료하지 못했습니다.",
        "evidence_mismatch": "원문 인용 불일치: 모델의 인용을 원문에서 확인하지 못했습니다.",
        "json_format": "응답 형식 오류: 모델 응답을 JSON으로 읽지 못했습니다.",
        "output_validation": "작성 조건 미충족: 분석 문단이나 인용 형식이 작성 조건을 충족하지 않았습니다.",
        "timeout": "응답 시간 초과: 모델 서버가 제한 시간 안에 응답하지 않았습니다.",
        "connection": "연결 오류: 모델 서버에 연결하지 못했습니다.",
        "rate_limit": "요청 한도 초과: 잠시 후 다시 시도해 주세요.",
        "provider_http": "모델 서버 오류: 잠시 후 다시 시도해 주세요.",
        "provider_error": "모델 호출 오류: 모델 설정과 연결 상태를 확인해 주세요.",
        "input_validation": "본문 준비 실패: 분석 가능한 본문을 읽지 못했습니다.",
    }
    category = diagnostic.get("category")
    reason = reasons.get(category) if isinstance(category, str) else None
    if reason:
        st.caption(reason)


def _render_job(owner_id: str, *, current_corpus: Mapping[str, Any] | None = None) -> None:
    initial = _latest_owned_job(owner_id)
    polling = bool(initial and initial.get("status") == "running")

    @st.fragment(run_every=2.0 if polling else None)
    def render_progress() -> None:
        job = _latest_owned_job(owner_id)
        if not job:
            return
        status = str(job.get("status") or "")
        if polling and status in {"completed", "partial", "failed", "empty"}:
            # Recreate the fragment without its timer and refresh sidebar status.
            # Draft scope widgets survive the single terminal app refresh.
            st.rerun(scope="app")
        if status == "running":
            completed = int(job.get("completed") or 0)
            total = int(job.get("total") or 0)
            phase_label = {
                "preparing": "본문 준비",
                "analyzing": "리포트 분석",
                "synthesizing": "종합 작성",
            }.get(job.get("phase"), "대상 확인")
            st.info(f"{phase_label} · {completed}/{total}건 처리")
            if total:
                st.progress(min(completed / total, 1.0))
            st.caption("다른 화면으로 이동하거나 새로고침해도 이 작업은 계속됩니다.")
            return
        if status == "empty":
            st.info("이 범위에는 분석 가능한 리포트가 없습니다. 준비 제외 자료가 있다면 미리보기에서 사유를 확인해 주세요.")
            result = job.get("result")
            if isinstance(result, Mapping):
                _render_result(result, current_corpus=current_corpus)
            return
        if status == "failed":
            result = job.get("result")
            _render_job_failure(job)
            if isinstance(result, Mapping):
                _render_result(result, current_corpus=current_corpus)
            _render_manual_retry(job, owner_id, partial=False)
            return
        result = job.get("result")
        if isinstance(result, Mapping):
            _render_result(result, current_corpus=current_corpus)
        if status == "partial":
            _render_manual_retry(job, owner_id, partial=True)

    render_progress()


def render_synthesis_page() -> None:
    """Render the complete company/date selection, preview, job, and result UI."""
    owner_id = _session_owner_id()
    st.header("기업 리포트 종합")
    st.caption(
        "선택 기간의 리포트를 근거로 기간 시작 시점의 인식, 주요 전개와 동인, "
        "종료 시점의 상황을 연결해 해당 기업에 대한 새로운 흐름 리포트를 작성합니다."
    )

    try:
        companies = report_corpus.list_companies()
    except (OSError, RuntimeError, ValueError):
        st.error("회사 목록을 불러오지 못했습니다. 데이터 상태를 확인해 주세요.")
        _render_job(owner_id)
        return
    if not companies:
        st.info("종합할 수 있는 기업 리포트가 아직 없습니다. 데이터 업데이트 후 다시 확인해 주세요.")
        _render_job(owner_id)
        return

    active = _latest_owned_job(owner_id)
    _restore_scope_widgets(companies, active)
    today = date.today()
    with st.form("synthesis_scope_form", enter_to_submit=False):
        company = st.selectbox("회사", companies, key=_COMPANY_KEY)
        preset = st.radio(
            "발행 기간",
            ("최근 1개월", "최근 3개월", "직접 선택"),
            horizontal=True,
            key=_PRESET_KEY,
        )
        # Initialize only missing state. Passing value as well would trigger
        # Streamlit's duplicate-default warning when a saved scope is restored.
        if _DATE_RANGE_KEY not in st.session_state:
            st.session_state[_DATE_RANGE_KEY] = _preset_range("최근 1개월", today=today)
        selected_range = st.date_input(
            "발행일 범위 (시작일과 종료일 포함)",
            max_value=today,
            key=_DATE_RANGE_KEY,
        )
        st.caption("날짜 입력은 '직접 선택'에만 적용됩니다. 조건을 모두 선택한 뒤 조회를 눌러 주세요.")
        submitted = st.form_submit_button("조회", type="primary", width="stretch")

    if submitted:
        # Draft widget values become the active scope only on explicit submit.
        st.session_state.pop(_PREVIEW_KEY, None)
        st.session_state.pop(_PREVIEW_ERROR_KEY, None)
        start_date, end_date = (_normalized_range(selected_range) if preset == "직접 선택"
                                else _preset_range(str(preset), today=today))
        if start_date is not None and end_date is not None:
            st.session_state[_SCOPE_KEY] = {
                "company": company, "preset": preset,
                "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            }
        error = None
        if not company:
            error = "회사를 선택해 주세요."
        elif start_date is None or end_date is None:
            error = "시작일과 종료일을 모두 선택해 주세요."
        elif start_date > end_date:
            error = "시작일은 종료일보다 늦을 수 없습니다."
        elif end_date > today:
            error = "종료일은 오늘보다 늦을 수 없습니다."
        if error:
            st.session_state[_PREVIEW_ERROR_KEY] = error
        else:
            try:
                st.session_state[_PREVIEW_KEY] = report_corpus.load_report_corpus(
                    str(company), start_date.isoformat(), end_date.isoformat()
                )
            except ValueError as exc:
                st.session_state[_PREVIEW_ERROR_KEY] = str(exc)
            except (OSError, RuntimeError):
                st.session_state[_PREVIEW_ERROR_KEY] = "대상 리포트를 확인하지 못했습니다. 데이터 상태를 확인해 주세요."

    corpus = st.session_state.get(_PREVIEW_KEY)
    error = st.session_state.get(_PREVIEW_ERROR_KEY)
    if error:
        st.warning(error)
    elif corpus is None:
        st.info("회사와 기간을 선택한 뒤 조회를 눌러 대상 리포트를 확인해 주세요.")

    ready_count = 0
    workload_error = None
    if corpus is not None:
        st.caption(f"조회 조건: {_result_scope(corpus)} · 이 조건으로 종합을 생성합니다.")
        counts = _corpus_counts(corpus)
        ready_count = counts["ready"]
        metric_columns = st.columns(4)
        for column, label, key in zip(
            metric_columns,
            ("확인된 자료", "본문 준비", "준비 제외", "증권사"),
            ("total", "ready", "excluded", "brokers"),
        ):
            unit = "곳" if key == "brokers" else "건"
            column.metric(label, f"{counts[key]}{unit}")
        st.caption(
            "준비된 본문을 직접 읽어 새 리포트를 작성합니다. 문서별 요약으로 나열하지 않으며, "
            "PDF 표 안에만 있는 수치는 빠질 수 있습니다."
        )
        for warning in corpus.get("warnings") or []:
            st.warning(str(warning))
        if counts["excluded"]:
            with st.expander(f"준비 제외 자료 ({counts['excluded']}건)"):
                for item in corpus.get("excluded") or []:
                    if isinstance(item, Mapping):
                        st.write(
                            f"{item.get('report_date') or '-'} · {item.get('broker') or '-'} · "
                            f"{item.get('title') or _document_file_name(item)} — {item.get('reason') or '본문 준비 안 됨'}"
                        )
        if counts["total"] == 0:
            st.info("선택한 기간에 확인된 자료가 없습니다. 이는 발행된 리포트가 없다는 뜻으로 단정할 수 없습니다.")
            available = corpus.get("available_period")
            if isinstance(available, Mapping):
                st.caption(
                    f"{corpus.get('company')}의 저장된 검색용 자료: "
                    f"{available.get('start_date')} ~ {available.get('end_date')} · "
                    f"{available.get('documents')}건. 선택한 발행일 범위를 확인해 주세요."
                )
        elif ready_count == 0:
            st.warning("확인된 자료는 있지만 종합할 본문이 준비되지 않았습니다. 제외 사유를 확인해 주세요.")
        workload_error = _workload_error(corpus)
        if workload_error:
            st.warning(workload_error + ". 기간을 줄여 주세요.")

    # History selection identifies the displayed result, not the owner's live
    # job. The unique running job is always the latest submission for an owner.
    job_running = bool(active and active.get("status") == "running") or any(
        job.get("status") == "running" for job in synthesis_jobs.list_jobs(owner_id, limit=1)
    )
    if st.button(
        "종합 생성",
        type="primary",
        width="stretch",
        disabled=bool(error or corpus is None or ready_count == 0 or workload_error or job_running),
    ):
        try:
            job_id = synthesis_jobs.start_job(
                str(corpus["company"]), str(corpus["start_date"]), str(corpus["end_date"]), owner_id=owner_id
            )
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
        else:
            st.session_state[_JOB_KEY] = job_id
            st.rerun(scope="app")
    if job_running:
        st.caption("이미 이 사용자 세션에서 종합 작업을 실행 중입니다. 중복 작업은 시작하지 않습니다.")

    # A stored preview is fixed at confirmation time. Only a fresh lookup may
    # replace the result's independent source-change check.
    _render_job(owner_id, current_corpus=corpus if submitted else None)
