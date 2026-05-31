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
