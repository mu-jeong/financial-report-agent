from src.core import db_manager
from src.core.db_manager import parse_filename


def test_parse_filename_accepts_current_five_part_rule():
    parsed = parse_filename("company_2026-02-05_삼성전자_미래에셋증권_HBM 전망.pdf")

    assert parsed == {
        "report_type": "company",
        "report_date": "2026-02-05",
        "target_name": "삼성전자",
        "broker": "미래에셋증권",
        "title": "HBM 전망",
    }


def test_parse_filename_preserves_underscores_inside_title():
    parsed = parse_filename("company_2026-02-05_삼성전자_미래에셋증권_HBM_AI 전망.pdf")

    assert parsed is not None
    assert parsed["title"] == "HBM_AI 전망"


def test_parse_filename_rejects_invalid_shape_or_date():
    assert parse_filename("삼성전자.pdf") is None
    assert parse_filename("company_2026-99-99_삼성전자_미래에셋증권_제목.pdf") is None
    assert parse_filename("company_2026-02-05_삼성전자_미래에셋증권_제목.txt") is None


def test_embedding_failure_reason_and_extraction_engine_are_recorded_and_cleared_on_success(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    monkeypatch.setattr(db_manager, "DB_PATH", str(db_path))

    db_manager.init_db()
    db_manager.upsert_report("company_2026-02-05_삼성전자_미래에셋증권_HBM 전망.pdf")

    db_manager.mark_embedding_failed(
        "company_2026-02-05_삼성전자_미래에셋증권_HBM 전망.pdf",
        "ValueError: Extracted text is empty",
        extraction_engine="opendataloader",
    )

    pending = db_manager.fetch_unembedded()
    assert pending[0]["embedding_last_error"] == "ValueError: Extracted text is empty"
    assert pending[0]["embedding_last_attempt_at"]
    assert pending[0]["embedding_extraction_engine"] == "opendataloader"

    db_manager.mark_embedded(
        "company_2026-02-05_삼성전자_미래에셋증권_HBM 전망.pdf",
        extraction_engine="opendataloader",
    )

    row = db_manager.fetch_all()[0]
    assert row["is_embedded"] == 1
    assert row["embedding_last_error"] is None
    assert row["embedding_last_attempt_at"] is None
    assert row["embedding_extraction_engine"] == "opendataloader"
