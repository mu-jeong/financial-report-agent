import json
import sys
import types

from src.core import compare_pdf_extractors
from src.core.compare_pdf_extractors import build_metrics, run_pdf_extraction_comparison, summarize
from src.core.pdf_extraction import (
    drop_markdown_tables,
    normalize_engine,
    text_from_opendataloader_json_without_tables,
)
from src.core import pdf_extraction


def test_build_metrics_flags_markdown_tables_and_numeric_noise():
    text = "# Header\n\n삼성전자 실적 개선 전망\n\n| A | B |\n| 1 | 2 |\n\n123 456 789"

    metrics = build_metrics(text)

    assert metrics["char_count"] == len(text)
    assert metrics["header_lines"] == 1
    assert metrics["pipe_table_lines"] == 2
    assert metrics["numeric_line_ratio"] > 0
    assert metrics["korean_line_ratio"] > 0


def test_summarize_groups_success_and_errors_by_engine():
    rows = [
        {
            "requested_engine": "pymupdf",
            "used_engine": "pymupdf",
            "status": "ok",
            "elapsed_sec": 1.0,
            "char_count": 100,
            "block_count": 3,
            "numeric_line_ratio": 0.1,
            "korean_line_ratio": 0.9,
        },
        {
            "requested_engine": "opendataloader",
            "used_engine": "",
            "status": "error",
            "elapsed_sec": 0.2,
            "error": "missing java",
        },
    ]

    summary = summarize(rows)

    assert summary["pymupdf"]["success"] == 1
    assert summary["pymupdf"]["avg_char_count"] == 100
    assert summary["opendataloader"]["errors"] == 1


def test_normalize_engine_accepts_opendataloader():
    assert normalize_engine(" OpenDataLoader ") == "opendataloader"
    assert normalize_engine("pdf-to-markdown") == "pdf-to-markdown"
    assert normalize_engine("PSPDFKit") == "pdf-to-markdown"
    assert normalize_engine("nutrient") == "pdf-to-markdown"
    assert normalize_engine("datalab-marker") == "marker"


def test_compare_config_engine_accepts_pdf_to_markdown_alias():
    assert compare_pdf_extractors.config_engine("pspdfkit") == "pdf-to-markdown"


def test_pdf_to_markdown_cli_extraction_uses_stdout(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    captured = {}

    monkeypatch.setattr(
        pdf_extraction.shutil,
        "which",
        lambda name: "pdf-to-markdown.exe" if name == "pdf-to-markdown" else None,
    )

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="PSPDFKit markdown", stderr="")

    monkeypatch.setattr(pdf_extraction.subprocess, "run", fake_run)

    assert pdf_extraction._extract_pspdfkit_pdf_to_markdown(pdf_path) == "PSPDFKit markdown"
    assert captured["args"] == ["pdf-to-markdown.exe", str(pdf_path)]
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["timeout"] == 300


def test_drop_markdown_tables_removes_pipe_and_html_tables():
    text = """# Summary

Keep this paragraph.

| Metric | Value |
| --- | ---: |
| Revenue | 100 |
| OP | 20 |

<table><tr><td>hidden table</td></tr></table>

Keep final paragraph.
"""

    cleaned = drop_markdown_tables(text)

    assert "Keep this paragraph." in cleaned
    assert "Keep final paragraph." in cleaned
    assert "| Metric | Value |" not in cleaned
    assert "Revenue" not in cleaned
    assert "<table>" not in cleaned
    assert "hidden table" not in cleaned


def test_opendataloader_extraction_drops_tables_before_return(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    class FakeDocument:
        page_content = json.dumps(
            {
                "kids": [
                    {"type": "heading", "content": "Title"},
                    {
                        "type": "table",
                        "id": 7,
                        "rows": [
                            {
                                "type": "table row",
                                "cells": [
                                    {
                                        "type": "table cell",
                                        "kids": [{"type": "paragraph", "content": "table value"}],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "type": "caption",
                        "linked content id": 7,
                        "content": "table caption",
                    },
                    {"type": "paragraph", "content": "Narrative text"},
                ]
            },
            ensure_ascii=False,
        )

    class FakeLoader:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            assert kwargs["format"] == "json"

        def load(self):
            return [FakeDocument()]

    fake_module = types.ModuleType("langchain_opendataloader_pdf")
    fake_module.OpenDataLoaderPDFLoader = FakeLoader
    monkeypatch.setitem(sys.modules, "langchain_opendataloader_pdf", fake_module)
    monkeypatch.setattr(pdf_extraction, "_ensure_java_on_path", lambda: None)

    text = pdf_extraction._extract_opendataloader_markdown(pdf_path)

    assert "Narrative text" in text
    assert "Title" in text
    assert "table value" not in text
    assert "table caption" not in text


def test_opendataloader_json_text_extractor_skips_table_nodes():
    payload = {
        "kids": [
            {"type": "paragraph", "content": "본문 설명"},
            {
                "type": "table",
                "id": 1,
                "rows": [
                    {
                        "type": "table row",
                        "cells": [
                            {
                                "type": "table cell",
                                "kids": [{"type": "paragraph", "content": "매출액 2025 480.7"}],
                            }
                        ],
                    }
                ],
            },
            {"type": "paragraph", "content": "다음 설명"},
        ]
    }

    text = text_from_opendataloader_json_without_tables(payload)

    assert text == "본문 설명\n\n다음 설명"
    assert "매출액" not in text


def test_drop_markdown_tables_removes_plain_text_financial_table_blocks():
    text = """투자 포인트는 실적 회복입니다.

GS 외 2 인 58.62 국민연금공단 6.06

###### Consensus Data

2025 2026 매출액(십억원) 480.7 636.9 영업이익(십억원) 76.0 125.1 순이익(십억원) 47.5 86.8 EPS(원) 1,592 2,944 BPS(원) 44,089 47,406

본문 설명은 유지되어야 합니다.
"""

    cleaned = drop_markdown_tables(text)

    assert "투자 포인트는 실적 회복입니다." in cleaned
    assert "본문 설명은 유지되어야 합니다." in cleaned
    assert "Consensus Data" not in cleaned
    assert "국민연금공단" not in cleaned
    assert "EPS" not in cleaned


def test_extract_pdf_text_drops_tables_before_openrouter_indexing_path(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        pdf_extraction,
        "_extract_pdf_text",
        lambda pdf_path, engine: "제목\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n본문 서술 내용입니다",
    )

    result = pdf_extraction.extract_pdf_text(pdf_path, "pymupdf", clean=True)

    assert result.text == "본문 서술 내용입니다"
    assert "| A | B |" not in result.text
    assert "| 1 | 2 |" not in result.text


def test_extract_pdf_text_drops_tables_even_for_raw_comparison_path(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        pdf_extraction,
        "_extract_pdf_text",
        lambda pdf_path, engine: "Before\n\n<table><tr><td>table value</td></tr></table>\n\nAfter",
    )

    result = pdf_extraction.extract_pdf_text(pdf_path, "marker", clean=False)

    assert result.text == "Before\n\nAfter"
    assert "table value" not in result.text


def test_run_pdf_extraction_comparison_persists_artifacts(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def fake_compare_extractors(files, engines, *, raw, sample_dir=None, sample_chars=4000):
        assert [path.name for path in files] == ["sample.pdf"]
        assert engines == ["pymupdf"]
        assert raw is False
        return [
            {
                "file_name": "sample.pdf",
                "file_path": str(pdf_path),
                "requested_engine": "pymupdf",
                "used_engine": "pymupdf",
                "status": "ok",
                "elapsed_sec": 0.1,
                "char_count": 120,
                "block_count": 2,
                "numeric_line_ratio": 0.1,
                "korean_line_ratio": 0.8,
            }
        ]

    monkeypatch.setattr(compare_pdf_extractors, "compare_extractors", fake_compare_extractors)

    result = run_pdf_extraction_comparison(
        [str(pdf_path)],
        ["pymupdf"],
        output_dir=tmp_path / "out",
        run_id="test-run",
    )

    assert result["run_id"] == "test-run"
    assert result["summary"]["pymupdf"]["success"] == 1
    assert (tmp_path / "out" / "test-run.csv").exists()
    assert (tmp_path / "out" / "test-run.json").exists()
