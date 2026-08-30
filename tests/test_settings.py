from datetime import date

from src.configs.settings import (
    BASE_DIR,
    CONFIG_SPECS,
    get_config_value,
    quickstart_env_updates,
    render_env_example,
    resolve_retrieval_path_settings,
)


def test_vector_comparison_execution_mode_is_not_an_environment_setting():
    assert "VECTOR_COMPARISON_EXECUTION_MODE" not in CONFIG_SPECS


def test_chunk_overlap_is_not_an_environment_setting():
    assert "CHUNK_OVERLAP" not in CONFIG_SPECS
    assert "CHUNK_OVERLAP" not in render_env_example()


def test_runtime_config_rejects_invalid_vector_retrieval_concurrency():
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["VECTOR_RETRIEVAL_CONCURRENCY"] = "0"
    result = subprocess.run(
        [sys.executable, "-c", "import src.configs.config"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "VECTOR_RETRIEVAL_CONCURRENCY" in result.stderr


def test_get_config_value_parses_typed_environment(monkeypatch):
    monkeypatch.setenv("CRAWLER_LOOKBACK_DAYS", "14")
    monkeypatch.setenv("USE_RERANKER", "yes")
    monkeypatch.setenv("RERANK_TIMEOUT", "12.5")
    monkeypatch.setenv("MONITORING_MODE", "on")
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    monkeypatch.setenv("MONITORING_ARTIFACT_ROOT", "/tmp/monitoring-artifacts")
    monkeypatch.setenv("ISSUE_REPORT_REMOTE_ENABLED", "off")
    monkeypatch.setenv("ISSUE_REPORT_INGEST_URL", "https://example.supabase.co/functions/v1/issue-report-ingest")
    monkeypatch.setenv("DATA_ROOT", "/tmp/eval")
    monkeypatch.setenv("RERANK_CACHE_DIR", "/tmp/model-cache")
    monkeypatch.setenv("COMPANY_INDUSTRY_DATA_PATH", "/tmp/eval/listed_company_industries.csv")
    monkeypatch.setenv("VECTOR_RETRIEVAL_CONCURRENCY", "3")

    assert get_config_value("CRAWLER_LOOKBACK_DAYS") == 14
    assert get_config_value("USE_RERANKER") is True
    assert get_config_value("RERANK_TIMEOUT") == 12.5
    assert get_config_value("MONITORING_MODE") is True
    assert get_config_value("DEPLOYMENT_ENVIRONMENT") == "production"
    assert get_config_value("MONITORING_ARTIFACT_ROOT") == "/tmp/monitoring-artifacts"
    assert get_config_value("ISSUE_REPORT_REMOTE_ENABLED") is False
    assert get_config_value("ISSUE_REPORT_INGEST_URL") == "https://example.supabase.co/functions/v1/issue-report-ingest"
    assert get_config_value("DATA_ROOT") == "/tmp/eval"
    assert get_config_value("RERANK_CACHE_DIR") == "/tmp/model-cache"
    assert get_config_value("COMPANY_INDUSTRY_DATA_PATH") == "/tmp/eval/listed_company_industries.csv"
    assert get_config_value("VECTOR_RETRIEVAL_CONCURRENCY") == 3


def test_pdf_extraction_engine_can_be_set_with_friendly_env_names(monkeypatch):
    monkeypatch.setenv("PDF_EXTRACTION_ENGINE", "docling")
    monkeypatch.setenv("PDF_EXTRACTION_FALLBACK_ENGINE", "opendataloader")
    monkeypatch.setenv("UNEMBEDDED_PDF_EXTRACTION_ENGINE", "pdf-to-markdown")

    assert get_config_value("PDF_EXTRACTION_ENGINE") == "docling"
    assert get_config_value("PDF_EXTRACTION_FALLBACK_ENGINE") == "opendataloader"
    assert get_config_value("UNEMBEDDED_PDF_EXTRACTION_ENGINE") == "pdf-to-markdown"


def test_pdf_extraction_engine_accepts_legacy_env_names(monkeypatch):
    monkeypatch.delenv("PDF_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("PDF_EXTRACTION_FALLBACK_ENGINE", raising=False)
    monkeypatch.delenv("UNEMBEDDED_PDF_EXTRACTION_ENGINE", raising=False)
    monkeypatch.setenv("EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setenv("EXTRACTION_FALLBACK_ENGINE", "docling")
    monkeypatch.setenv("UNEMBEDDED_EXTRACTION_ENGINE", "opendataloader")

    assert get_config_value("PDF_EXTRACTION_ENGINE") == "pymupdf"
    assert get_config_value("PDF_EXTRACTION_FALLBACK_ENGINE") == "docling"
    assert get_config_value("UNEMBEDDED_PDF_EXTRACTION_ENGINE") == "opendataloader"


def test_get_config_value_uses_defaults_for_missing_or_blank(monkeypatch):
    monkeypatch.delenv("CRAWLER_LOOKBACK_DAYS", raising=False)
    monkeypatch.delenv("PDF_EXTRACTION_FALLBACK_ENGINE", raising=False)
    monkeypatch.delenv("UNEMBEDDED_PDF_EXTRACTION_ENGINE", raising=False)
    monkeypatch.delenv("MONITORING_MODE", raising=False)
    monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("ISSUE_REPORT_REMOTE_ENABLED", raising=False)
    monkeypatch.setenv("ISSUE_REPORT_INGEST_URL", "")
    monkeypatch.setenv("ISSUE_REPORT_PUBLISHABLE_KEY", "")
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("SEARCH_CANDIDATE_MULTIPLIER", raising=False)
    monkeypatch.delenv("VECTOR_RETRIEVAL_CONCURRENCY", raising=False)
    monkeypatch.setenv("CRAWLER_TARGET_DATE", "")
    monkeypatch.setenv("UNEMBEDDED_EXTRACTION_ENGINE", "")

    assert get_config_value("CRAWLER_LOOKBACK_DAYS") == 7
    assert get_config_value("CRAWLER_TARGET_DATE") == date.today().isoformat()
    assert get_config_value("PDF_EXTRACTION_FALLBACK_ENGINE") == ""
    assert get_config_value("UNEMBEDDED_PDF_EXTRACTION_ENGINE") == ""
    assert get_config_value("MONITORING_MODE") is False
    assert get_config_value("DEPLOYMENT_ENVIRONMENT") == "development"
    assert get_config_value("ISSUE_REPORT_REMOTE_ENABLED") is True
    assert get_config_value("ISSUE_REPORT_INGEST_URL") == (
        "https://rjjnhvoontxpimhiabou.supabase.co/functions/v1/"
        "issue-report-ingest"
    )
    assert get_config_value("ISSUE_REPORT_PUBLISHABLE_KEY") == (
        "sb_publishable_O5bQ-b9VvY1fcwDz0IQlPg_prsFM87B"
    )
    assert get_config_value("DATA_ROOT") == str(BASE_DIR / "data")
    assert get_config_value("SEARCH_CANDIDATE_MULTIPLIER") == 1
    assert get_config_value("VECTOR_RETRIEVAL_CONCURRENCY") == 5


def test_data_root_is_the_canonical_retrieval_authority(tmp_path):
    root = tmp_path / "native data"

    paths = resolve_retrieval_path_settings(
        {
            "DATA_ROOT": str(root),
        }
    )

    assert paths.data_root == root.resolve()
    assert paths.rerank_cache_dir == root.resolve() / "cache" / "flashrank"


def test_rerank_cache_can_be_configured_independently(tmp_path):
    root = tmp_path / "native"
    cache = tmp_path / "cache elsewhere"

    paths = resolve_retrieval_path_settings(
        {
            "DATA_ROOT": str(root),
            "RERANK_CACHE_DIR": str(cache),
        }
    )

    assert paths.rerank_cache_dir == cache.resolve()


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
    assert "# --- Optional path overrides (leave blank to use defaults) ---" in content
    assert "Optional override for the canonical Native V2 retrieval root." in content
    assert "REPORT_PDF_DIR=" in content
    assert "DATA_ROOT=" in content
    assert "COMPANY_INDUSTRY_DATA_PATH=" in content
    assert "MONITORING_MODE=false" in content
    assert "DEPLOYMENT_ENVIRONMENT=development" in content
    assert "MONITORING_SUPABASE_URL=https://rjjnhvoontxpimhiabou.supabase.co" in content
    assert "MONITORING_SUPABASE_PUBLISHABLE_KEY=sb_publishable_" in content
    assert (
        "MONITORING_OPERATOR_API_URL=https://rjjnhvoontxpimhiabou.supabase.co/"
        "functions/v1/issue-report-operator"
    ) in content
    assert "MONITORING_ARTIFACT_ROOT=" in content
    assert "ISSUE_REPORT_REMOTE_ENABLED=true" in content
    assert (
        "ISSUE_REPORT_INGEST_URL=https://rjjnhvoontxpimhiabou.supabase.co/"
        "functions/v1/issue-report-ingest"
    ) in content
    assert (
        "ISSUE_REPORT_PUBLISHABLE_KEY="
        "sb_publishable_O5bQ-b9VvY1fcwDz0IQlPg_prsFM87B"
    ) in content
    assert "ISSUE_REPORT_OUTBOX_DIR=" in content
    assert "SEARCH_CANDIDATE_MULTIPLIER=1" in content
    assert "VECTOR_COMPARISON_EXECUTION_MODE" not in content
    assert "VECTOR_RETRIEVAL_CONCURRENCY=5" in content
    assert "RERANK_CANDIDATE_MULTIPLIER" not in content


def test_env_example_file_matches_rendered_specs():
    assert (BASE_DIR / ".env.example").read_text(encoding="utf-8-sig") == render_env_example()
