"""Portable Markdown exports for saved chat and synthesis results."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from src.core.chat_ui_helpers import escape_numeric_tildes_for_markdown
from src.utils import citations


_PATH_SEPARATOR_RE = re.compile(r"[\\/]+")
_WEB_URL_RE = re.compile(r"(?i)https?://[^\s<>()]+")
_LOCAL_MARKDOWN_LINK_RE = re.compile(
    r"!?\[([^\]\n]*)\]\(\s*"
    r"(file:(?:/{1,3})?[^)\s]+|[a-z]:[\\/][^)\s]+|\\\\[^)\s]+|/[^)\s]+)"
    r"(?:\s+['\"][^'\"]*['\"])?\s*\)",
    re.IGNORECASE,
)
_FILE_URI_RE = re.compile(r"(?i)(?<!\w)file:(?:/{1,3})?[^\s<>()]+")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<!\w)[a-z]:[\\/]+(?:[^\\/\s]+[\\/]+)*([^\\/\s]+)"
)
_UNC_ABSOLUTE_PATH_RE = re.compile(
    r"(?<!\w)(?:\\\\|//)[^\\/\s]+[\\/]+(?:[^\\/\s]+[\\/]+)*([^\\/\s]+)"
)
_UNIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:/])/(?:[^/\s]+/)*([^/\s]+)")


def _text(value: Any, fallback: str = "-") -> str:
    rendered = portable_markdown(value).strip()
    return rendered or fallback


def _file_name(value: Any) -> str:
    """Return only a portable basename, never a local directory."""
    parts = [part for part in _PATH_SEPARATOR_RE.split(str(value or "").strip()) if part]
    return PurePosixPath(parts[-1]).name if parts else "-"


def _portable_free_text(value: Any) -> str:
    text = str(value or "").strip()
    web_urls: list[str] = []

    def protect_web_url(match: re.Match[str]) -> str:
        web_urls.append(match.group(0))
        return f"\x00WEBURL{len(web_urls) - 1}\x00"

    text = _WEB_URL_RE.sub(protect_web_url, text)
    text = _FILE_URI_RE.sub(lambda match: _file_name(match.group(0)), text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub(lambda match: _file_name(match.group(0)), text)
    text = _UNC_ABSOLUTE_PATH_RE.sub(lambda match: _file_name(match.group(0)), text)
    text = _UNIX_ABSOLUTE_PATH_RE.sub(lambda match: _file_name(match.group(0)), text)
    text = escape_numeric_tildes_for_markdown(text)
    for index, url in enumerate(web_urls):
        text = text.replace(f"\x00WEBURL{index}\x00", url)
    return text


def portable_markdown(value: Any) -> str:
    """Remove local-only links and paths while preserving portable Markdown."""
    text = str(value or "")

    def replace_local_link(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        file_name = _file_name(match.group(2))
        return f"{label} ({file_name})" if label else file_name

    return _portable_free_text(_LOCAL_MARKDOWN_LINK_RE.sub(replace_local_link, text))


def _source_line(info: Mapping[str, Any], rank: int | None = None) -> str:
    prefix = f"- [{rank}] " if rank is not None else "- "
    return (
        f"{prefix}{_text(info.get('report_date'))} · "
        f"{_text(info.get('broker'))} · {_text(info.get('title'), '제목 없음')}"
    )


def _chat_body_and_sources(message: Mapping[str, Any]) -> tuple[str, list[tuple[int, Mapping[str, Any]]], str]:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}
    sources = list(metadata.get("selected_sources") or metadata.get("rerank_info") or [])
    contract = metadata.get("citation_contract")
    validation = citations.validate_citation_contract(sources, contract)
    status = validation["status"]
    content = str(message.get("content") or "")

    if status == citations.CITATION_CONTRACT_VALID:
        source_count = int(validation["document_count"])
        body = citations.remove_unavailable_document_references(content, source_count=source_count)
        groups = citations.group_sources_by_persisted_document_rank(sources, contract)
        rows = [(int(group["document_rank"]), group["info"]) for group in groups]
    elif status == citations.CITATION_CONTRACT_LEGACY:
        groups = citations.group_sources_by_document(sources)
        body = citations.remove_unavailable_citations(content, source_count=len(sources))
        body = citations.normalize_citation_ranks(body, citations.document_rank_aliases(sources))
        body = citations.remove_unavailable_citations(body, source_count=len(groups))
        rows = [(rank, group["info"]) for rank, group in enumerate(groups, 1)]
        source_count = len(rows)
    else:
        # A marked but invalid contract cannot safely map a number to a document.
        return portable_markdown(content), [], status

    used = citations.extract_citation_ranks(body, source_count=source_count)
    if used:
        rows = [(rank, info) for rank, info in rows if rank in used]
    return portable_markdown(body), rows, status


def _chat_scope_lines(metadata: Mapping[str, Any]) -> list[str]:
    scope = metadata.get("search_scope") if isinstance(metadata.get("search_scope"), Mapping) else {}
    filters = metadata.get("search_filters")
    if not isinstance(filters, Mapping):
        filters = scope.get("search_filters") if isinstance(scope.get("search_filters"), Mapping) else {}
    lines: list[str] = []
    targets = filters.get("target_names") or filters.get("target_name")
    if isinstance(targets, (list, tuple)):
        target_text = ", ".join(_text(item) for item in targets if str(item or "").strip())
    else:
        target_text = str(targets or "").strip()
    if target_text:
        lines.append(f"- 대상 기업: {target_text}")
    start = str(filters.get("report_date_start") or filters.get("start_date") or "").strip()
    end = str(filters.get("report_date_end") or filters.get("end_date") or "").strip()
    if start or end:
        lines.append(f"- 적용 기간: {start or '-'} ~ {end or '-'}")
    raw_report_types = filters.get("report_types") or filters.get("report_type")
    report_types = raw_report_types if isinstance(raw_report_types, (list, tuple)) else [raw_report_types]
    report_types = [str(item).strip() for item in report_types if str(item or "").strip()]
    if report_types:
        labels = {"company": "기업 리포트", "industry": "산업 리포트", "economy": "경제 리포트"}
        lines.append(f"- 자료 유형: {', '.join(labels.get(item, item) for item in report_types)}")
    brokers = filters.get("brokers") or filters.get("broker")
    if isinstance(brokers, (list, tuple)):
        broker_text = ", ".join(_text(item) for item in brokers if str(item or "").strip())
    else:
        broker_text = str(brokers or "").strip()
    if broker_text:
        lines.append(f"- 증권사: {broker_text}")
    file_names = scope.get("file_names") or filters.get("file_names") or []
    if isinstance(file_names, str):
        file_names = [file_names]
    portable_names = [_file_name(item) for item in file_names if str(item or "").strip()]
    if portable_names:
        lines.append(f"- 문서 범위: {', '.join(dict.fromkeys(portable_names))}")
    notice = str(metadata.get("scope_notice") or "").strip()
    if notice and not lines:
        lines.append(f"- 적용 조건: {notice}")
    return lines


def build_chat_markdown(message: Mapping[str, Any]) -> str:
    """Build a portable answer export without changing the saved message."""
    body, rows, contract_status = _chat_body_and_sources(message)
    metadata = message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}
    generated_at = str(metadata.get("generated_at") or "").strip()
    recorded_at = str(message.get("created_at") or "").strip()
    lines = ["# Chat 답변", ""]
    if generated_at:
        lines.extend([f"- 생성 시점: {generated_at}"])
    elif recorded_at:
        lines.extend([f"- 답변 기록 시각: {recorded_at}"])
    lines.extend(_chat_scope_lines(metadata))
    if len(lines) > 2:
        lines.append("")
    lines.extend([body.rstrip(), "", "## 출처", ""])
    if contract_status == citations.CITATION_CONTRACT_INVALID:
        lines.append("저장된 인용 정보를 검증할 수 없어 문서 번호와 출처의 대응을 내보내지 않았습니다.")
        raw_sources = metadata.get("selected_sources") or metadata.get("rerank_info") or []
        safe_sources = [item for item in raw_sources if isinstance(item, dict)]
        if safe_sources:
            lines.extend(["", "### 번호 없는 저장 출처", ""])
            lines.extend(
                _source_line(group["info"])
                for group in citations.group_sources_by_document(safe_sources)
            )
    elif rows:
        lines.extend(_source_line(info, rank) for rank, info in rows)
    else:
        lines.append("표시할 문서 단위 출처가 없습니다.")
    return "\n".join(lines).rstrip() + "\n"


def _synthesis_source_rows(result: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    sources = list(result.get("selected_sources") or [])
    contract = result.get("citation_contract")
    if citations.validate_citation_contract(sources, contract)["status"] != citations.CITATION_CONTRACT_VALID:
        return []
    return [
        (int(group["document_rank"]), group["info"])
        for group in citations.group_sources_by_persisted_document_rank(sources, contract)
    ]


def _document_identity(document: Mapping[str, Any]) -> str:
    for key in ("report_uid", "document_uid", "uid"):
        value = str(document.get(key) or "").strip()
        if value:
            return f"uid:{value}"
    return "meta:" + "|".join(
        str(document.get(key) or "").strip()
        for key in ("report_date", "broker", "title", "file_name")
    )


def build_synthesis_markdown(
    result: Mapping[str, Any],
    *,
    body: str | None = None,
    recorded_at: str | None = None,
    source_changed: bool = False,
    source_unverified: bool = False,
) -> str:
    """Build a portable completed or partial synthesis export."""
    status = str(result.get("status") or "")
    if status not in {"completed", "partial"}:
        raise ValueError("완료 또는 부분 완료 종합만 내보낼 수 있습니다.")
    report_body = portable_markdown(result.get("content") if body is None else body)
    counts = result.get("counts") if isinstance(result.get("counts"), Mapping) else {}
    coverage = result.get("coverage") if isinstance(result.get("coverage"), Mapping) else {}
    generated_at = str(result.get("generated_at") or "").strip()
    recorded_at = str(recorded_at or "").strip()
    lines = [f"# {_text(result.get('company'))} 기간 내 기업 흐름 리포트", ""]
    if generated_at:
        lines.append(f"- 생성 시점: {generated_at}")
    elif recorded_at:
        lines.append(f"- 결과 기록 시각: {recorded_at}")
    lines.extend([
        f"- 적용 기업: {_text(result.get('company'))}",
        f"- 적용 기간: {_text(result.get('start_date'))} ~ {_text(result.get('end_date'))}",
        "- 자료 유형: 기업 리포트",
        f"- 결과 상태: {'부분 완료' if status == 'partial' else '완료'}",
    ])
    if counts:
        lines.append(
            f"- 자료 범위: 전체 {int(counts.get('total') or 0)}건 · "
            f"본문 준비 {int(counts.get('completed') or 0)}건 · "
            f"준비 실패 {int(counts.get('failed') or 0)}건 · "
            f"제외 {int(counts.get('excluded') or 0)}건"
        )
    if coverage:
        lines.append(
            f"- 본문 반영: 준비 {int(coverage.get('prepared') or 0)}건 · "
            f"인용 {int(coverage.get('cited') or 0)}건 · "
            f"인용 미채택 {int(coverage.get('uncited') or 0)}건"
        )
    lines.extend(["", report_body.rstrip(), ""])

    limitations: list[str] = []
    if int(counts.get("failed") or 0):
        limitations.append(f"본문을 준비하지 못한 문서가 {int(counts.get('failed') or 0)}건 있습니다.")
    if int(counts.get("excluded") or 0):
        limitations.append(f"준비 대상에서 제외된 문서가 {int(counts.get('excluded') or 0)}건 있습니다.")
    if int(coverage.get("uncited") or 0):
        limitations.append(f"준비됐지만 최종 인용 근거로 채택되지 않은 문서가 {int(coverage.get('uncited') or 0)}건 있습니다.")
    if result.get("validation_warnings"):
        limitations.append("근거 조건을 충족하지 못한 일부 비교 문단이 결과에서 제외되었습니다.")
    for warning in result.get("warnings") or []:
        warning_text = portable_markdown(warning)
        if warning_text:
            limitations.append(warning_text)
    if source_unverified:
        limitations.append("현재 자료 구성을 확인할 수 없어 생성 당시 원문 범위와의 일치 여부를 검증하지 못했습니다.")
    elif source_changed:
        limitations.append("종합 생성 후 대상 자료가 변경되었습니다. 현재 자료와 결과의 원문 범위가 다를 수 있습니다.")
    if limitations:
        lines.extend(["## 결과 한계", "", *(f"- {item}" for item in limitations), ""])

    source_rows = _synthesis_source_rows(result)
    lines.extend(["## 출처", ""])
    if source_rows:
        lines.extend(_source_line(info, rank) for rank, info in source_rows)
    elif result.get("selected_sources"):
        lines.append("저장된 인용 정보를 검증할 수 없어 문서 번호와 출처의 대응을 내보내지 않았습니다.")
    else:
        lines.append("표시할 인용 문서가 없습니다.")

    cited_identities = {_document_identity(info) for _, info in source_rows}
    other_documents = [
        item for item in result.get("documents") or []
        if isinstance(item, Mapping) and _document_identity(item) not in cited_identities
    ]
    if other_documents:
        lines.extend(["", "### 그 외 대상 문서", ""])
        for document in other_documents:
            state = portable_markdown(
                document.get("reason")
                or document.get("error")
                or document.get("status")
                or "대상 문서"
            )
            lines.append(f"{_source_line(document)} · 상태: {state}")
    return "\n".join(lines).rstrip() + "\n"


def safe_export_stem(value: Any, fallback: str) -> str:
    """Create a short cross-platform filename stem while preserving Unicode."""
    stem = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", str(value or "").strip())
    stem = re.sub(r"\s+", "-", stem).strip(" .-")
    stem = re.sub(r"-{2,}", "-", stem)
    return (stem[:80].rstrip(" .-") or fallback)
