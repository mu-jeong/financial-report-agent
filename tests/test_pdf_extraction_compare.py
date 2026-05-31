from src.core.compare_pdf_extractors import build_metrics, summarize
from src.core.pdf_extraction import normalize_engine


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
