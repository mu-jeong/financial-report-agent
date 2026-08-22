import csv
from pathlib import Path

from src.core.company_industry import lookup_companies_by_industry


def _write_company_industry_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["company_name", "industry", "main_products"])
        writer.writeheader()
        writer.writerows(rows)


def test_lookup_companies_by_industry_matches_industry_and_products(tmp_path):
    data_path = tmp_path / "listed_company_industries.csv"
    _write_company_industry_csv(
        data_path,
        [
            {"company_name": "삼성전자", "industry": "반도체 제조업", "main_products": "메모리 반도체"},
            {"company_name": "SK하이닉스", "industry": "전자부품 제조업", "main_products": "반도체기억장치"},
            {"company_name": "현대차", "industry": "자동차 제조업", "main_products": "승용차"},
        ],
    )

    result = lookup_companies_by_industry("반도체", data_path=data_path)

    assert result["term"] == "반도체"
    assert result["matched_company_count"] == 2
    assert [row["company_name"] for row in result["matches"]] == ["삼성전자", "SK하이닉스"]
    assert result["source_url"] == "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage"
