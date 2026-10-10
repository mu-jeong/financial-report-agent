from __future__ import annotations

import sqlite3
import json
from datetime import date, timedelta

import pytest

from src.core.report_corpus import ReportCorpusError, list_companies, load_report_corpus, validate_scope
from tests.retrieval.test_retrieval_delta_reader import _DeltaChange, _publish_delta, _publish_upsert_delta
from tests.retrieval.test_retrieval_repository import _create_catalog, _digest


def test_company_dates_and_type_use_all_parent_body_not_only_vector_child(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    corpus = load_report_corpus("Alpha", "2026-07-01", "2026-07-03", data_root=tmp_path, catalog_path=catalog)
    assert [doc["report_date"] for doc in corpus["documents"]] == ["2026-07-01", "2026-07-03"]
    assert all(doc["report_type"] == "company" for doc in corpus["documents"])
    assert corpus["documents"][0]["parents"][0]["content"] == "prefix::native child body 1::suffix"
    assert corpus["revision"]["publication_generation"] == 7
    assert list_companies(data_root=tmp_path, catalog_path=catalog) == ["Alpha", "Beta"]


def test_company_is_sql_parameter_and_date_boundaries_are_inclusive(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    assert not load_report_corpus("Alpha' OR 1=1 --", "2026-07-01", "2026-07-31", catalog_path=catalog)["documents"]
    assert len(load_report_corpus("Alpha", "2026-07-03", "2026-07-03", catalog_path=catalog)["documents"]) == 1


def test_empty_scope_reports_available_active_dates_without_expanding_selected_period(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    corpus = load_report_corpus("Alpha", "2026-09-01", "2026-09-30", catalog_path=catalog)
    assert corpus["documents"] == [] and corpus["excluded"] == []
    assert corpus["start_date"] == "2026-09-01" and corpus["end_date"] == "2026-09-30"
    assert corpus["available_period"] == {
        "start_date": "2026-07-01", "end_date": "2026-07-03", "documents": 2}


def test_ready_delta_replacement_and_weekend_are_visible_without_vector_search(tmp_path, monkeypatch):
    catalog, _ = _create_catalog(tmp_path)
    uid = _publish_upsert_delta(catalog, tmp_path, sequence=1, canonical_path="reports/company-1.pdf",
                                body="updated complete body", vector=(1.0, 2.0))
    # The feature must work even if a vector artifact has disappeared.
    for artifact in tmp_path.rglob("*.faiss"):
        artifact.unlink()
    corpus = load_report_corpus("Alpha", "2026-07-01", "2026-08-01", catalog_path=catalog)
    assert len(corpus["documents"]) == 2
    updated = next(doc for doc in corpus["documents"] if doc["report_uid"] == uid)
    assert updated["report_date"] == "2026-08-01"  # Saturday.
    assert "updated complete body" in updated["parents"][0]["content"]
    assert corpus["revision"]["delta_generation"] == 1
    assert all(doc["report_uid"] != _digest("report-1") for doc in corpus["documents"])


def test_deleted_report_is_not_resurrected_from_historical_rows(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    _publish_delta(catalog, tmp_path, sequence=1, changes=(_DeltaChange("delete", "reports/company-1.pdf"),))
    corpus = load_report_corpus("Alpha", "2026-07-01", "2026-07-31", catalog_path=catalog)
    assert [doc["report_uid"] for doc in corpus["documents"]] == [_digest("report-3")]
    assert corpus["available_period"] == {
        "start_date": "2026-07-03", "end_date": "2026-07-03", "documents": 1}


def test_all_parents_of_active_profile_are_loaded_and_other_profiles_are_excluded(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    with sqlite3.connect(catalog) as connection:
        connection.execute("""INSERT INTO embedding_profiles SELECT 'profile-2', ?, model, dimension,
            metric, normalization, prefix_template, extractor, parent_policy_json,
            child_policy_json, created_at FROM embedding_profiles WHERE profile_id='profile-1'""", (_digest("profile-2"),))
        for profile, order, body in [("profile-1", 1, "last paragraph full body"), ("profile-2", 0, "wrong profile body")]:
            connection.execute("""INSERT INTO retrieval_parents
                (parent_uid, report_id, profile_id, parent_order, content, content_sha256)
                VALUES (?, 1, ?, ?, ?, ?)""", (_digest(profile + body), profile, order, body, _digest(body)))
    doc = load_report_corpus("Alpha", "2026-07-01", "2026-07-01", catalog_path=catalog)["documents"][0]
    assert [parent["parent_order"] for parent in doc["parents"]] == [0, 1]
    assert doc["parents"][-1]["content"] == "last paragraph full body"


@pytest.mark.parametrize("limit", ["documents", "body_chars", "chunks"])
def test_overlarge_scope_is_rejected_before_body_or_pdf_processing(tmp_path, monkeypatch, limit):
    from src.core import report_corpus as module

    catalog, _ = _create_catalog(tmp_path)
    with sqlite3.connect(catalog) as connection:
        if limit == "documents":
            connection.execute("DROP VIEW active_reports")
            connection.execute("CREATE VIEW active_reports AS SELECT * FROM reports")
            existing = connection.execute(
                """SELECT count(DISTINCT report.report_id)
                   FROM active_reports AS report
                   JOIN retrieval_parents AS parent ON parent.report_id=report.report_id
                   WHERE report.report_type='company' AND report.target_name='Alpha'
                     AND report.report_date BETWEEN '2026-07-01' AND '2026-07-31'
                     AND parent.profile_id='profile-1'"""
            ).fetchone()[0]
            for index in range(201 - existing):
                report_id = 1000 + index
                uid = _digest(f"preflight-report-{index}")
                body = f"body-{index}"
                connection.execute(
                    """INSERT INTO reports (report_id, report_uid, canonical_relative_path,
                           source_sha256, retrieval_metadata_sha256, report_type, report_date,
                           target_name, title, broker)
                       VALUES (?, ?, ?, ?, ?, 'company', '2026-07-02', 'Alpha', 'Title', 'Broker')""",
                    (report_id, uid, f"reports/preflight-{index}.pdf",
                     _digest(f"source-{index}"), _digest(f"metadata-{index}")),
                )
                connection.execute(
                    """INSERT INTO retrieval_parents
                           (parent_uid, report_id, profile_id, parent_order, content, content_sha256)
                       VALUES (?, ?, 'profile-1', 0, ?, ?)""",
                    (_digest(f"workload-parent-{report_id}"), report_id, body, _digest(body)),
                )
        elif limit == "body_chars":
            body = "x" * 2_000_001
            connection.execute(
                """INSERT INTO retrieval_parents
                       (parent_uid, report_id, profile_id, parent_order, content, content_sha256)
                   VALUES (?, 1, 'profile-1', 99, ?, ?)""",
                (_digest("oversized-workload-parent"), body, _digest(body)),
            )
        else:
            for parent_order in range(1, 500):
                body = f"x{parent_order}"
                connection.execute(
                    """INSERT INTO retrieval_parents
                           (parent_uid, report_id, profile_id, parent_order, content, content_sha256)
                       VALUES (?, 1, 'profile-1', ?, ?, ?)""",
                    (_digest(f"chunk-parent-{parent_order}"), parent_order, body, _digest(body)),
                )

    def unexpected_processing(*args, **kwargs):
        raise AssertionError("body/PDF processing ran before workload validation")

    monkeypatch.setattr(module, "_metadata", unexpected_processing)
    monkeypatch.setattr(module, "_inventory", unexpected_processing)
    with pytest.raises(ValueError) as raised:
        load_report_corpus("Alpha", "2026-07-01", "2026-07-31",
                           data_root=tmp_path, catalog_path=catalog, pdf_dir=tmp_path)
    assert str(raised.value) == (
        "분석 범위가 실행 한도를 초과했습니다. 기간을 줄여 주세요 "
        "(최대 200건, 본문 2,000,000자, 500구간)."
    )


def test_whitespace_only_parents_remain_excluded_from_workload_count(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    with sqlite3.connect(catalog) as connection:
        connection.execute("DROP VIEW active_reports")
        connection.execute("CREATE VIEW active_reports AS SELECT * FROM reports")
        for index in range(201):
            report_id = 2000 + index
            uid = _digest(f"whitespace-report-{index}")
            body = "\t\u00a0"
            connection.execute(
                """INSERT INTO reports (report_id, report_uid, canonical_relative_path,
                       source_sha256, retrieval_metadata_sha256, report_type, report_date,
                       target_name, title, broker)
                   VALUES (?, ?, ?, ?, ?, 'company', '2026-07-02', 'Whitespace', 'Title', 'Broker')""",
                (report_id, uid, f"reports/whitespace-{index}.pdf",
                 _digest(f"whitespace-source-{index}"), _digest(f"whitespace-metadata-{index}")),
            )
            connection.execute(
                """INSERT INTO retrieval_parents
                       (parent_uid, report_id, profile_id, parent_order, content, content_sha256)
                   VALUES (?, ?, 'profile-1', 0, ?, ?)""",
                (_digest(f"whitespace-parent-{index}"), report_id, body, _digest(body)),
            )

    corpus = load_report_corpus("Whitespace", "2026-07-01", "2026-07-31",
                                data_root=tmp_path, catalog_path=catalog)
    assert corpus["documents"] == []
    assert len(corpus["excluded"]) == 201


def test_downloaded_unprepared_company_can_be_selected_and_is_explicitly_excluded(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    (downloaded / "company_2026-07-04_Gamma_Broker_Result.pdf").write_bytes(b"pdf placeholder")
    companies = list_companies(data_root=tmp_path, catalog_path=catalog, pdf_dir=downloaded)
    assert "Gamma" in companies
    corpus = load_report_corpus("Gamma", "2026-07-01", "2026-07-31", data_root=tmp_path,
                                catalog_path=catalog, pdf_dir=downloaded)
    assert corpus["documents"] == []
    assert len(corpus["excluded"]) == 1
    assert "아직 준비" in corpus["excluded"][0]["reason"]


def test_fingerprint_stable_until_matching_document_changes(tmp_path):
    catalog, _ = _create_catalog(tmp_path)
    kwargs = {"catalog_path": catalog, "data_root": tmp_path}
    original = load_report_corpus("Alpha", "2026-07-01", "2026-08-01", **kwargs)
    _publish_upsert_delta(catalog, tmp_path, sequence=1, canonical_path="reports/company-2.pdf",
                         body="Beta becomes Alpha in fixture", vector=(1.0, 2.0))
    changed = load_report_corpus("Alpha", "2026-07-01", "2026-08-01", **kwargs)
    assert original["fingerprint"] != changed["fingerprint"]
    assert original["documents"][0]["parents"][0]["content"] == "prefix::native child body 1::suffix"


@pytest.mark.parametrize("company,start,end", [("", "2026-07-01", "2026-07-02"),
    ("Alpha", "bad", "2026-07-02"), ("Alpha", "2026-07-02", "2026-07-01"),
    ("Alpha", "2026-07-01", (date.today() + timedelta(days=1)).isoformat())])
def test_invalid_scope_is_rejected_before_opening_catalog(company, start, end):
    with pytest.raises(ValueError):
        validate_scope(company, start, end)


def test_missing_catalog_is_read_only_and_not_created(tmp_path):
    with pytest.raises(ReportCorpusError):
        load_report_corpus("Alpha", "2026-07-01", "2026-07-31", data_root=tmp_path)
    assert not (tmp_path / "retrieval/v2/catalog.sqlite3").exists()


def test_corrupt_catalog_is_reported_as_user_facing_corpus_error(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a database")
    with pytest.raises(ReportCorpusError):
        load_report_corpus("Alpha", "2026-07-01", "2026-07-31", catalog_path=path)


def test_latest_failed_delta_supersedes_base_exclusion_before_scope_filtering(tmp_path):
    old_uid, new_uid = _digest("excluded-old"), _digest("excluded-new")
    manifest = json.dumps({"reports": [{"report_uid": old_uid, "status": "excluded", "reason_code": "old_failure"}]})
    catalog, _ = _create_catalog(tmp_path, source_manifest_json=manifest)
    # Install the delta views with a valid, unrelated no-vector deletion.
    _publish_delta(catalog, tmp_path, sequence=1, changes=(_DeltaChange("delete", "reports/industry-5.pdf"),))
    with sqlite3.connect(catalog) as connection:
        for rid, uid, company, day in [(6, old_uid, "Alpha", "2026-07-01"), (7, new_uid, "Beta", "2026-09-01")]:
            connection.execute("""INSERT INTO reports (report_id, report_uid, canonical_relative_path,
                source_sha256, retrieval_metadata_sha256, report_type, report_date, target_name, title, broker)
                VALUES (?, ?, 'reports/excluded.pdf', ?, ?, 'company', ?, ?, 'Failed', 'Broker')""",
                (rid, uid, _digest(uid + "source"), _digest(uid + "meta"), day, company))
        segment = _digest("latest-failure")
        connection.execute("""INSERT INTO retrieval_delta_segments (segment_id, base_snapshot_id,
            base_publication_generation, sequence, relative_path, file_sha256, size_bytes, dimension, metric, ntotal)
            VALUES (?, 'snapshot-1', 7, 2, NULL, NULL, 0, 2, 'l2', 0)""", (segment,))
        connection.execute("""INSERT INTO retrieval_delta_reports
            (segment_id, canonical_relative_path, action, report_uid, reason_code)
            VALUES (?, 'reports/excluded.pdf', 'failed', ?, 'new_failure')""", (segment, new_uid))
        connection.execute("UPDATE retrieval_delta_segments SET state='ready' WHERE segment_id=?", (segment,))
    old_scope = load_report_corpus("Alpha", "2026-07-01", "2026-07-31", catalog_path=catalog)
    new_scope = load_report_corpus("Beta", "2026-09-01", "2026-09-30", catalog_path=catalog)
    assert old_scope["excluded"] == []
    assert len(new_scope["excluded"]) == 1
    assert new_scope["excluded"][0]["report_uid"] == new_uid
    assert "new_failure" in new_scope["excluded"][0]["reason"]


def test_publication_during_body_read_cannot_mix_document_versions(tmp_path, monkeypatch):
    from src.core import report_corpus as module

    catalog, _ = _create_catalog(tmp_path)
    original_metadata = module._metadata
    published = False

    def publish_during_read(row):
        nonlocal published
        if not published:
            published = True
            _publish_upsert_delta(catalog, tmp_path, sequence=1, canonical_path="reports/company-3.pdf",
                                 body="new version during read", vector=(2.0, 3.0))
        return original_metadata(row)

    monkeypatch.setattr(module, "_metadata", publish_during_read)
    corpus = load_report_corpus("Alpha", "2026-07-01", "2026-08-01", catalog_path=catalog)
    assert corpus["revision"]["delta_generation"] == 0
    assert corpus["documents"][-1]["report_uid"] == _digest("report-3")
    assert "native child body 3" in corpus["documents"][-1]["parents"][0]["content"]
    next_corpus = load_report_corpus("Alpha", "2026-07-01", "2026-08-01", catalog_path=catalog)
    assert next_corpus["revision"]["delta_generation"] == 1
    assert "new version during read" in next_corpus["documents"][-1]["parents"][0]["content"]
