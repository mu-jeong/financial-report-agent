import json
import sqlite3
from pathlib import Path

import pytest

from src.configs.settings import BASE_DIR

DATASET_PATH = BASE_DIR / "tests" / "fixtures" / "evaluation_dataset.json"
SNAPSHOT_ROOT = BASE_DIR / "tests" / "fixtures" / "eval_snapshot"
pytestmark = pytest.mark.skipif(
    not DATASET_PATH.is_file()
    or not (SNAPSHOT_ROOT / "manifest.json").is_file()
    or not (SNAPSHOT_ROOT / "reports.db").is_file(),
    reason=(
        "evaluation dataset and fixed snapshot are intentionally deferred "
        "until the source data is complete"
    ),
)
REQUIRED_SOURCE_FIELDS = {
    "report_type",
    "report_date",
    "target_name",
    "broker",
    "title",
    "file_name",
}


def load_dataset() -> dict:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8-sig"))


def test_evaluation_dataset_has_expected_schema():
    dataset = load_dataset()

    assert dataset["name"] == "finance_llm_local_eval_dataset"
    assert dataset["version"] >= 2
    assert dataset["selection_criteria"]
    assert dataset["stability_policy"]["policy"] == "fixed_baseline_until_change_reason"
    assert dataset["stability_policy"]["allowed_change_reasons"]
    assert dataset["cases"]

    case_ids = [case["id"] for case in dataset["cases"]]
    assert len(case_ids) == len(set(case_ids))

    case_types = {case["type"] for case in dataset["cases"]}
    assert {"vectordb_retrieval", "rdb_aggregate"} <= case_types

    criteria_ids = {criterion["id"] for criterion in dataset["selection_criteria"]}
    assert {
        "local_reproducibility",
        "route_coverage",
        "filter_coverage",
        "ranking_challenge",
        "monitoring_metric_relevance",
        "privacy_and_size",
    } <= criteria_ids

    for case in dataset["cases"]:
        assert case["question"].strip()
        assert case["expected_route"] in {"vectordb", "rdb"}
        assert case["checks"]
        assert case["selection_reason"].strip()
        assert case["criteria_tags"]
        assert set(case["criteria_tags"]) <= criteria_ids
        assert case["monitoring_dimensions"]

        if case["type"] == "vectordb_retrieval":
            assert case["expected_sources"]
            for source in case["expected_sources"]:
                assert REQUIRED_SOURCE_FIELDS <= set(source)
                assert isinstance(source["id"], int)
                assert source["file_name"].endswith(".pdf")

        if case["type"] == "rdb_aggregate":
            expected_result = case["expected_result"]
            assert expected_result
            assert expected_result["columns"]
            assert expected_result["rows"]
            assert case["expected_sql_intent"].strip()


def test_evaluation_dataset_sources_exist_in_fixed_snapshot():
    db_path = SNAPSHOT_ROOT / "reports.db"
    assert db_path.is_file()

    dataset = load_dataset()
    expected_files = {
        source["file_name"]
        for case in dataset["cases"]
        for source in case.get("expected_sources", [])
    }

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT file_name
            FROM reports
            WHERE file_name IN ({",".join("?" for _ in expected_files)})
            """,
            sorted(expected_files),
        ).fetchall()

    existing_files = {row[0] for row in rows}
    assert expected_files == existing_files
