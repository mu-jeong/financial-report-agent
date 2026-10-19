from copy import deepcopy

import pytest

from src.core import report_export
from src.utils import citations


def _source(rank: int, file_name: str, *, uid: str | None = None) -> dict:
    return {
        "rank": rank,
        "report_uid": uid or file_name,
        "target_name": "삼성전자",
        "report_date": f"2026-09-{rank:02d}",
        "broker": "테스트증권",
        "title": f"리포트 {rank}",
        "file_name": file_name,
    }


def test_chat_export_deduplicates_chunks_and_matches_document_citation_numbers():
    raw = [_source(1, "same.pdf"), _source(2, "same.pdf"), _source(3, "other.pdf")]
    sources, contract = citations.annotate_document_citation_sources(raw)
    message = {
        "content": "첫 문서 [1], 둘째 문서 [2]",
        "created_at": "2026-10-19T12:00:00+00:00",
        "metadata": {
            "selected_sources": sources,
            "citation_contract": contract,
            "search_scope": {
                "search_filters": {
                    "target_name": "삼성전자",
                    "report_date_start": "2026-09-01",
                    "report_date_end": "2026-09-30",
                    "report_types": ["company", "industry"],
                    "brokers": ["테스트증권", "다른증권"],
                },
                "file_names": [r"C:\\private\\same.pdf", "other.pdf"],
            },
        },
    }
    saved = deepcopy(message)

    exported = report_export.build_chat_markdown(message)

    assert exported.count("[1] 2026-09-01 · 테스트증권 · 리포트 1") == 1
    assert exported.count("[2] 2026-09-03 · 테스트증권 · 리포트 3") == 1
    assert ".pdf" not in exported.split("## 출처", 1)[1]
    assert "same.pdf" in exported and "other.pdf" in exported
    assert "대상 기업: 삼성전자" in exported
    assert "적용 기간: 2026-09-01 ~ 2026-09-30" in exported
    assert "자료 유형: 기업 리포트, 산업 리포트" in exported
    assert "증권사: 테스트증권, 다른증권" in exported
    assert "문서 범위: same.pdf, other.pdf" in exported
    assert "C:\\private" not in exported
    assert "답변 기록 시각:" in exported
    assert message == saved


def test_chat_export_renders_sources_as_separate_markdown_list_items():
    raw = [_source(1, "first.pdf"), _source(2, "second.pdf")]
    sources, contract = citations.annotate_document_citation_sources(raw)

    exported = report_export.build_chat_markdown({
        "content": "첫 문서 [1], 둘째 문서 [2]",
        "metadata": {"selected_sources": sources, "citation_contract": contract},
    })
    source_lines = exported.split("## 출처", 1)[1].splitlines()

    assert "- [1] 2026-09-01 · 테스트증권 · 리포트 1" in source_lines
    assert "- [2] 2026-09-02 · 테스트증권 · 리포트 2" in source_lines
    assert not any(line.startswith("[1]") or line.startswith("[2]") for line in source_lines)


def test_chat_export_escapes_numeric_tildes_without_mutating_or_changing_code():
    body = (
        "매출 5~10%, 전망 ~23%, ~~삭제 표시~~\n\n"
        "인라인 `값 ~42%`\n\n"
        "~~~python\nvalue = '~99%'\n~~~"
    )
    message = {"content": body, "metadata": {}}
    saved = deepcopy(message)

    exported = report_export.build_chat_markdown(message)

    assert "매출 5\\~10%, 전망 \\~23%, ~~삭제 표시~~" in exported
    assert "인라인 `값 ~42%`" in exported
    assert "~~~python\nvalue = '~99%'\n~~~" in exported
    assert message == saved


def test_chat_export_preserves_numeric_tildes_inside_http_urls():
    exported = report_export.build_chat_markdown({
        "content": (
            "전망 ~23%와 원문 https://example.test/~23/report.pdf, "
            "[링크](https://example.test/~24/detail)를 확인한다."
        ),
        "metadata": {},
    })

    assert "전망 \\~23%" in exported
    assert "https://example.test/~23/report.pdf" in exported
    assert "[링크](https://example.test/~24/detail)" in exported
    assert "https://example.test/\\~" not in exported


def test_chat_export_normalizes_legacy_chunk_ranks_without_inventing_sources():
    sources = [_source(1, "same.pdf"), _source(2, "other.pdf"), _source(3, "same.pdf")]
    exported = report_export.build_chat_markdown({
        "content": "중복 청크 [3]와 다른 문서 [2]",
        "metadata": {"selected_sources": sources},
    })

    assert "중복 청크 [1]와 다른 문서 [2]" in exported
    assert exported.count("[1] 2026-09-01 · 테스트증권 · 리포트 1") == 1
    assert exported.count("[2] 2026-09-02 · 테스트증권 · 리포트 2") == 1


def test_invalid_chat_citation_contract_preserves_body_and_does_not_map_numbers():
    exported = report_export.build_chat_markdown({
        "content": "검증할 수 없는 번호 [9]는 그대로 둔다.",
        "metadata": {
            "selected_sources": [_source(1, r"C:\\private\\report.pdf")],
            "citation_contract": {"version": 999},
        },
    })

    assert "번호 [9]는 그대로" in exported
    assert "문서 번호와 출처의 대응을 내보내지 않았습니다" in exported
    assert "[1] 2026-09-01 · 테스트증권" not in exported
    assert "번호 없는 저장 출처" in exported and "2026-09-01 · 테스트증권 · 리포트 1" in exported
    assert "report.pdf" not in exported
    assert "C:\\private" not in exported

    source_lines = exported.split("### 번호 없는 저장 출처", 1)[1].splitlines()
    assert "- 2026-09-01 · 테스트증권 · 리포트 1" in source_lines
    assert not any(line.startswith("- [1]") for line in source_lines)


def test_chat_body_removes_local_links_and_absolute_paths_but_keeps_web_links_and_citations():
    source, contract = citations.annotate_document_citation_sources([_source(1, "report.pdf")])
    exported = report_export.build_chat_markdown({
        "content": (
            "근거 [1]\n\n"
            "[원문](file:///C:/private/report.pdf) · C:\\secret.pdf · "
            r"\\server\share\chart.png · /private/figure.png · "
            "[공식 자료](https://example.com/reports/view?id=1) · "
            "PBR < 1배 · 10/20 · 그림 1 · ![차트](https://example.com/chart.png)"
        ),
        "metadata": {"selected_sources": source, "citation_contract": contract},
    })

    assert "근거 [1]" in exported
    assert "원문 (report.pdf)" in exported
    assert "secret.pdf" in exported and "chart.png" in exported and "figure.png" in exported
    assert "file:" not in exported
    assert "C:\\" not in exported and "\\\\server" not in exported and "/private/" not in exported
    assert "[공식 자료](https://example.com/reports/view?id=1)" in exported
    assert "PBR < 1배 · 10/20 · 그림 1" in exported
    assert "![차트](https://example.com/chart.png)" in exported


def _partial_result() -> dict:
    cited = {
        **_source(1, r"D:\\reports\\삼성전자.pdf", uid="cited"),
        "passage_rank": 1,
        "document_rank": 1,
        "status": "prepared",
    }
    uncited = {
        **_source(2, "/srv/reports/미인용.pdf", uid="uncited"),
        "status": "prepared",
        "reason": "최종 인용 미채택",
    }
    failed = {
        **_source(3, "실패.pdf", uid="failed"),
        "status": "failed",
        "reason": "본문 읽기 실패",
    }
    return {
        "status": "partial",
        "company": "삼성전자",
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "content": "한글 본문과 근거 [1]",
        "selected_sources": [cited],
        "citation_contract": {
            "version": 2,
            "rank_kind": "document",
            "passage_count": 1,
            "document_count": 1,
        },
        "documents": [cited, uncited, failed],
        "counts": {"total": 3, "completed": 2, "failed": 1, "excluded": 0},
        "coverage": {"prepared": 2, "cited": 1, "uncited": 1},
        "validation_warnings": [{"section": "changes", "reason": "broker_changes"}],
        "warnings": [
            "수집 한도로 일부 자료가 제외될 수 있습니다.",
            "/srv/private/원문.pdf: 저장된 본문을 사용합니다.",
        ],
    }


def test_partial_synthesis_export_preserves_coverage_warnings_sources_and_unicode():
    result = _partial_result()
    saved = deepcopy(result)

    exported = report_export.build_synthesis_markdown(
        result,
        recorded_at="2026-10-19T12:34:56+00:00",
        source_changed=True,
    )

    assert "# 삼성전자 기간 내 기업 흐름 리포트" in exported
    assert "결과 기록 시각: 2026-10-19T12:34:56+00:00" in exported
    assert "결과 상태: 부분 완료" in exported
    assert "인용 미채택 1건" in exported
    assert "본문을 준비하지 못한 문서가 1건" in exported
    assert "일부 비교 문단이 결과에서 제외" in exported
    assert "수집 한도로 일부 자료가 제외될 수 있습니다." in exported
    assert "원문.pdf: 저장된 본문을 사용합니다." in exported
    assert "종합 생성 후 대상 자료가 변경" in exported
    assert "[1] 2026-09-01 · 테스트증권 · 리포트 1" in exported
    assert "리포트 2 · 상태: 최종 인용 미채택" in exported
    assert "리포트 3 · 상태: 본문 읽기 실패" in exported
    assert ".pdf" not in exported.split("## 출처", 1)[1]
    assert "D:\\reports" not in exported and "/srv/" not in exported
    assert result == saved


def test_synthesis_export_renders_multiple_sources_as_markdown_list_items():
    raw = [_source(1, "first.pdf"), _source(2, "second.pdf")]
    sources, contract = citations.annotate_document_citation_sources(raw)
    result = {
        "status": "completed",
        "company": "테스트기업",
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "content": "첫 문서 [1], 둘째 문서 [2]",
        "selected_sources": sources,
        "citation_contract": contract,
        "documents": sources,
    }

    exported = report_export.build_synthesis_markdown(result)
    source_lines = exported.split("## 출처", 1)[1].splitlines()

    assert "- [1] 2026-09-01 · 테스트증권 · 리포트 1" in source_lines
    assert "- [2] 2026-09-02 · 테스트증권 · 리포트 2" in source_lines
    assert not any(line.startswith("[1]") or line.startswith("[2]") for line in source_lines)


def test_synthesis_export_escapes_numeric_tildes_without_mutating_result():
    result = _partial_result()
    result["content"] = "영업이익률 5~10%, 목표 ~23%, ~~제외~~, `코드 ~42%` [1]"
    saved = deepcopy(result)

    exported = report_export.build_synthesis_markdown(result)

    assert "영업이익률 5\\~10%, 목표 \\~23%, ~~제외~~, `코드 ~42%` [1]" in exported
    assert result == saved


def test_synthesis_uses_recorded_generation_time_only_when_present():
    result = _partial_result()
    result["status"] = "completed"
    result["generated_at"] = "2026-10-19T13:00:00+00:00"
    exported = report_export.build_synthesis_markdown(
        result,
        recorded_at="2026-10-19T12:00:00+00:00",
    )
    assert "생성 시점: 2026-10-19T13:00:00+00:00" in exported
    assert "결과 기록 시각" not in exported


def test_synthesis_distinguishes_unverified_sources_from_changed_sources():
    exported = report_export.build_synthesis_markdown(
        _partial_result(), source_changed=True, source_unverified=True
    )
    assert "일치 여부를 검증하지 못했습니다" in exported
    assert "대상 자료가 변경되었습니다" not in exported


def test_synthesis_sanitizes_body_warning_and_document_state_local_paths():
    result = _partial_result()
    result["selected_sources"][0]["title"] = "[제목](file:///C:/private/source-title.pdf)"
    result["documents"][0]["title"] = result["selected_sources"][0]["title"]
    result["content"] = (
        "본문 [1] [local](file:///C:/private/report.pdf) "
        "C:\\secret.pdf /secret.pdf https://example.com/a.pdf"
    )
    result["warnings"] = ["[local](file:///C:/private/warning.pdf)"]
    result["documents"][1]["reason"] = r"\\server\share\uncited.pdf 처리 실패"

    exported = report_export.build_synthesis_markdown(result)

    assert "본문 [1] local (report.pdf) secret.pdf secret.pdf" in exported
    assert "https://example.com/a.pdf" in exported
    assert "local (warning.pdf)" in exported
    assert "제목 (source-title.pdf)" in exported
    assert "uncited.pdf 처리 실패" in exported
    for local_prefix in ("file:", "C:\\", "\\\\server", "/secret.pdf"):
        assert local_prefix not in exported


def test_synthesis_rejects_non_terminal_results():
    with pytest.raises(ValueError, match="완료 또는 부분 완료"):
        report_export.build_synthesis_markdown({"status": "failed", "content": "실패"})


def test_safe_export_stem_preserves_unicode_and_removes_path_characters():
    assert report_export.safe_export_stem(" 삼성전자: 9월/종합 ", "report") == "삼성전자-9월-종합"
