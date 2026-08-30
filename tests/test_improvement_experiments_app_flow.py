from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


def _write_harness(path: Path, pdf_root: Path) -> None:
    path.write_text(
        f"""
import apps.gui.monitoring_views as views

views.config_module.REPORT_PDF_DIR = {str(pdf_root)!r}
views.render_improvement_experiments_page()
""",
        encoding="utf-8",
    )


def test_improvement_experiments_page_renders_pdf_parsing_comparison_only(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "improvement_experiments_harness.py"
    _write_harness(harness, tmp_path / "pdfs")

    app = AppTest.from_file(str(harness))
    app.run(timeout=20)

    assert not app.exception
    assert [item.value for item in app.header] == ["개선 실험"]
    assert [item.value for item in app.subheader] == ["Parsing engine evaluation"]
    assert [item.label for item in app.text_area] == ["PDF file or directory paths"]
    assert [item.label for item in app.multiselect] == ["Engines"]
