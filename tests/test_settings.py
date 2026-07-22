from datetime import date

from src.configs.settings import BASE_DIR, get_config_value, quickstart_env_updates, render_env_example


def test_get_config_value_parses_typed_environment(monkeypatch):
    monkeypatch.setenv("CRAWLER_LOOKBACK_DAYS", "14")
    monkeypatch.setenv("USE_RERANKER", "yes")
    monkeypatch.setenv("RERANK_TIMEOUT", "12.5")
    monkeypatch.setenv("MONITORING_MODE", "on")
    monkeypatch.setenv("DB_PATH", "/tmp/eval/reports.db")
    monkeypatch.setenv("FAISS_DIR", "/tmp/eval/vector_db")
    monkeypatch.setenv("COMPANY_INDUSTRY_DATA_PATH", "/tmp/eval/listed_company_industries.csv")

    assert get_config_value("CRAWLER_LOOKBACK_DAYS") == 14
    assert get_config_value("USE_RERANKER") is True
    assert get_config_value("RERANK_TIMEOUT") == 12.5
    assert get_config_value("MONITORING_MODE") is True
    assert get_config_value("DB_PATH") == "/tmp/eval/reports.db"
    assert get_config_value("FAISS_DIR") == "/tmp/eval/vector_db"
    assert get_config_value("COMPANY_INDUSTRY_DATA_PATH") == "/tmp/eval/listed_company_industries.csv"


def test_pdf_extraction_engine_can_be_set_with_friendly_env_names(monkeypatch):
    monkeypatch.setenv("PDF_EXTRACTION_ENGINE", "docling")
    monkeypatch.setenv("PDF_EXTRACTION_FALLBACK_ENGINE", "marker")
    monkeypatch.setenv("UNEMBEDDED_PDF_EXTRACTION_ENGINE", "pdf-to-markdown")

    assert get_config_value("PDF_EXTRACTION_ENGINE") == "docling"
    assert get_config_value("PDF_EXTRACTION_FALLBACK_ENGINE") == "marker"
    assert get_config_value("UNEMBEDDED_PDF_EXTRACTION_ENGINE") == "pdf-to-markdown"


def test_pdf_extraction_engine_accepts_legacy_env_names(monkeypatch):
    monkeypatch.delenv("PDF_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("PDF_EXTRACTION_FALLBACK_ENGINE", raising=False)
    monkeypatch.delenv("UNEMBEDDED_PDF_EXTRACTION_ENGINE", raising=False)
    monkeypatch.setenv("EXTRACTION_ENGINE", "marker")
    monkeypatch.setenv("EXTRACTION_FALLBACK_ENGINE", "docling")
    monkeypatch.setenv("UNEMBEDDED_EXTRACTION_ENGINE", "opendataloader")

    assert get_config_value("PDF_EXTRACTION_ENGINE") == "marker"
    assert get_config_value("PDF_EXTRACTION_FALLBACK_ENGINE") == "docling"
    assert get_config_value("UNEMBEDDED_PDF_EXTRACTION_ENGINE") == "opendataloader"


def test_get_config_value_uses_defaults_for_missing_or_blank(monkeypatch):
    monkeypatch.delenv("CRAWLER_LOOKBACK_DAYS", raising=False)
    monkeypatch.delenv("PDF_EXTRACTION_FALLBACK_ENGINE", raising=False)
    monkeypatch.delenv("UNEMBEDDED_PDF_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("MONITORING_MODE", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("FAISS_DIR", raising=False)
    monkeypatch.setenv("CRAWLER_TARGET_DATE", "")
    monkeypatch.setenv("UNEMBEDDED_EXTRACTION_ENGINE", "")

    assert get_config_value("CRAWLER_LOOKBACK_DAYS") == 7
    assert get_config_value("CRAWLER_TARGET_DATE") == date.today().isoformat()
    assert get_config_value("PDF_EXTRACTION_FALLBACK_ENGINE") == ""
    assert get_config_value("UNEMBEDDED_PDF_EXTRACTION_ENGINE") == ""
    assert get_config_value("MONITORING_MODE") is False
    assert get_config_value("DB_PATH") == str(BASE_DIR / "data" / "reports.db")
    assert get_config_value("FAISS_DIR") == str(BASE_DIR / "data" / "vector_db")


def test_pdf_extraction_fallback_can_be_disabled_with_an_explicit_blank(monkeypatch):
    monkeypatch.setenv("PDF_EXTRACTION_FALLBACK_ENGINE", "")

    assert get_config_value("PDF_EXTRACTION_FALLBACK_ENGINE") == ""


def test_quickstart_env_updates_use_run_date_and_shared_defaults(monkeypatch):
    for key in [
        "CRAWLER_LOOKBACK_DAYS",
        "CRAWLER_TARGET_COUNT",
        "CRAWLER_MAX_LOOKBACK_DAYS",
    ]:
        monkeypatch.delenv(key, raising=False)

    updates = quickstart_env_updates(date(2026, 6, 3))

    assert updates == {
        "CRAWLER_MODE": "LATEST",
        "CRAWLER_TARGET_DATE": "2026-06-03",
        "CRAWLER_LOOKBACK_DAYS": "7",
        "CRAWLER_TARGET_COUNT": "0",
        "CRAWLER_MAX_LOOKBACK_DAYS": "7",
    }


def test_render_env_example_contains_generated_defaults():
    content = render_env_example()

    assert "Generated from src/configs/settings.py" in content
    assert "OPENROUTER_API_KEY=your_openrouter_api_key_here" in content
    assert "CRAWLER_TARGET_DATE=" in content
    assert "CRAWLER_LOOKBACK_DAYS=7" in content
    assert "PDF_EXTRACTION_ENGINE=pymupdf" in content
    assert "PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader" in content
    assert "UNEMBEDDED_PDF_EXTRACTION_ENGINE=pymupdf" in content
    assert "REPORT_PDF_DIR=" in content
    assert "COMPANY_INDUSTRY_DATA_PATH=" in content
    assert "MONITORING_MODE=false" in content


def test_env_example_file_matches_rendered_specs():
    assert (BASE_DIR / ".env.example").read_text(encoding="utf-8-sig") == render_env_example()
