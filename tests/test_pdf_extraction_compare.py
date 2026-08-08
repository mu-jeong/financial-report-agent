import json
import sys
import types

import pytest

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


def test_normalize_engine_accepts_supported_engines_and_rejects_marker():
    assert normalize_engine(" OpenDataLoader ") == "opendataloader"
    assert normalize_engine(" docling ") == "docling"
    assert normalize_engine("pdf-to-markdown") == "pdf-to-markdown"
    assert normalize_engine("PSPDFKit") == "pdf-to-markdown"
    assert normalize_engine("nutrient") == "pdf-to-markdown"
    with pytest.raises(ValueError, match="Unsupported extraction engine"):
        normalize_engine("marker")
    with pytest.raises(ValueError, match="Unsupported extraction engine"):
        normalize_engine("datalab-marker")


def test_compare_config_engine_accepts_pdf_to_markdown_alias():
    assert compare_pdf_extractors.config_engine("pspdfkit") == "pdf-to-markdown"


def test_docling_extraction_uses_document_converter(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    captured = {}

    class FakeInputFormat:
        PDF = "pdf"

    class FakePdfPipelineOptions:
        def __init__(self):
            self.do_table_structure = True

    class FakePdfFormatOption:
        def __init__(self, *, pipeline_options):
            self.pipeline_options = pipeline_options

    class FakeDocument:
        def export_to_markdown(self):
            return "Docling markdown"

    class FakeResult:
        document = FakeDocument()

    class FakeConverter:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def convert(self, source):
            assert source == str(pdf_path)
            return FakeResult()

    fake_package = types.ModuleType("docling")
    fake_datamodel = types.ModuleType("docling.datamodel")
    fake_base_models = types.ModuleType("docling.datamodel.base_models")
    fake_base_models.InputFormat = FakeInputFormat
    fake_pipeline_options = types.ModuleType("docling.datamodel.pipeline_options")
    fake_pipeline_options.PdfPipelineOptions = FakePdfPipelineOptions
    fake_module = types.ModuleType("docling.document_converter")
    fake_module.DocumentConverter = FakeConverter
    fake_module.PdfFormatOption = FakePdfFormatOption
    monkeypatch.setitem(sys.modules, "docling", fake_package)
    monkeypatch.setitem(sys.modules, "docling.datamodel", fake_datamodel)
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models", fake_base_models)
    monkeypatch.setitem(sys.modules, "docling.datamodel.pipeline_options", fake_pipeline_options)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_module)

    assert pdf_extraction._extract_docling_markdown(pdf_path) == "Docling markdown"
    pdf_options = captured["kwargs"]["format_options"][FakeInputFormat.PDF].pipeline_options
    assert pdf_options.do_table_structure is False


def test_pymupdf_extraction_uses_loose_default_table_detection(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    captured = {"closed": False, "find_tables_kwargs": []}

    class FakeFinder:
        tables = [types.SimpleNamespace(bbox=(0, 20, 200, 80))]

    class FakePage:
        def find_tables(self, **kwargs):
            captured["find_tables_kwargs"].append(kwargs)
            return FakeFinder()

        def get_text(self, mode, **kwargs):
            assert mode == "blocks"
            assert kwargs == {}
            return [
                (0, 0, 200, 10, "narrative before", 0, 0),
                # Exactly 50% overlap is intentionally retained.
                (0, 0, 200, 40, "partially overlapping narrative", 1, 0),
                (0, 20, 200, 80, "table value 2026 100", 2, 0),
                (0, 90, 200, 110, "narrative after", 3, 0),
                (0, 0, 10, 10, "image metadata", 4, 1),
            ]

    class FakeDocument:
        def __iter__(self):
            return iter([FakePage()])

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(pdf_extraction.fitz, "open", lambda source: FakeDocument())

    text = pdf_extraction._extract_pymupdf_text(pdf_path)

    assert text == (
        "narrative before"
        "\n\npartially overlapping narrative"
        "\n\nnarrative after"
    )
    assert captured["closed"] is True
    assert captured["find_tables_kwargs"] == [{}]


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
    captured = {}

    def fake_run(input_path, output_dir, *, timeout_seconds):
        captured["input_path"] = input_path
        captured["timeout_seconds"] = timeout_seconds
        (output_dir / "sample.json").write_text(
            json.dumps(
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
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(pdf_extraction, "_run_opendataloader_cli", fake_run)
    monkeypatch.setattr(pdf_extraction, "_ensure_java_on_path", lambda: None)

    text = pdf_extraction._extract_opendataloader_markdown(pdf_path)

    assert captured == {
        "input_path": pdf_path,
        "timeout_seconds": 300,
    }
    assert "Narrative text" in text
    assert "Title" in text
    assert "table value" not in text
    assert "table caption" not in text


def test_opendataloader_process_timeout_is_reported_as_extraction_failure(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "stuck.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        raise pdf_extraction.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(pdf_extraction.subprocess, "run", fake_run)

    with pytest.raises(TimeoutError, match="timed out after 300 seconds"):
        pdf_extraction._run_opendataloader_process(
            ["java", "-jar", "opendataloader-pdf-cli.jar"],
            pdf_path=pdf_path,
            timeout_seconds=300,
        )

    assert captured["kwargs"]["timeout"] == 300
    assert captured["kwargs"]["capture_output"] is True


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

    result = pdf_extraction.extract_pdf_text(
        pdf_path,
        "opendataloader",
        clean=False,
    )

    assert result.text == "Before\n\nAfter"
    assert "table value" not in result.text


def test_extract_pdf_text_falls_back_from_pymupdf_to_configured_engine(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    calls = []

    def fake_extract(path, engine):
        calls.append((path, engine))
        if engine == "pymupdf":
            raise RuntimeError("primary failed")
        return "유효한 fallback 본문입니다."

    monkeypatch.setattr(pdf_extraction, "_extract_pdf_text", fake_extract)

    result = pdf_extraction.extract_pdf_text(
        pdf_path,
        "pymupdf",
        fallback_engine="opendataloader",
    )

    assert calls == [
        (pdf_path, "pymupdf"),
        (pdf_path, "opendataloader"),
    ]
    assert result.requested_engine == "pymupdf"
    assert result.used_engine == "opendataloader-fallback"
    assert result.text == "유효한 fallback 본문입니다."


def test_extract_pdf_text_does_not_call_fallback_when_pymupdf_succeeds(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    calls = []

    def fake_extract(path, engine):
        calls.append((path, engine))
        return "유효한 PyMuPDF 본문입니다."

    monkeypatch.setattr(pdf_extraction, "_extract_pdf_text", fake_extract)

    result = pdf_extraction.extract_pdf_text(
        pdf_path,
        "pymupdf",
        fallback_engine="opendataloader",
    )

    assert calls == [(pdf_path, "pymupdf")]
    assert result.used_engine == "pymupdf"


def test_extract_pdf_text_preserves_primary_failure_when_fallback_is_disabled(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    calls = []

    def fake_extract(path, engine):
        calls.append((path, engine))
        raise RuntimeError("primary failed")

    monkeypatch.setattr(pdf_extraction, "_extract_pdf_text", fake_extract)

    with pytest.raises(RuntimeError, match="primary failed"):
        pdf_extraction.extract_pdf_text(
            pdf_path,
            "pymupdf",
            allow_fallback=False,
            fallback_engine="opendataloader",
        )

    assert calls == [(pdf_path, "pymupdf")]


def test_extract_pdf_text_without_fallback_keyword_preserves_legacy_pymupdf_policy(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    calls = []

    def fake_extract(path, engine):
        calls.append((path, engine))
        if engine == "docling":
            raise RuntimeError("docling failed")
        return "legacy fallback text"

    monkeypatch.setattr(pdf_extraction.config, "EXTRACTION_FALLBACK_ENGINE", "opendataloader")
    monkeypatch.setattr(pdf_extraction, "_extract_pdf_text", fake_extract)

    result = pdf_extraction.extract_pdf_text(pdf_path, "docling")

    assert calls == [(pdf_path, "docling"), (pdf_path, "pymupdf")]
    assert result.used_engine == "pymupdf-fallback"


def test_extract_pdf_text_dual_failure_reports_both_engines(tmp_path, monkeypatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def fail_extract(_path, engine):
        raise RuntimeError(f"{engine} failed")

    monkeypatch.setattr(pdf_extraction, "_extract_pdf_text", fail_extract)

    with pytest.raises(RuntimeError) as raised:
        pdf_extraction.extract_pdf_text(
            pdf_path,
            "pymupdf",
            fallback_engine="opendataloader",
        )

    message = str(raised.value)
    assert "pymupdf extraction failed (pymupdf failed)" in message
    assert "fallback opendataloader extraction failed (opendataloader failed)" in message


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
