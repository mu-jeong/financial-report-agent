from datetime import date
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from src.core import issue_report_store, status as status_module
from src.core.data_update_jobs import build_crawler_env, missing_update_dates_by_category
from src.core.status import (
    format_duration,
    get_data_status,
    get_native_v2_data_status,
    list_unembedded_reports,
)
from src.retrieval.delta_schema import install_delta_schema
from tests.retrieval.test_retrieval_delta_reader import _DeltaChange, _publish_delta
from tests.retrieval.test_retrieval_repository import _create_catalog, _digest


def test_native_v2_monitoring_status_reports_missing_runtime_as_unavailable(tmp_path):
    source = Path(status_module.__file__).read_text(encoding="utf-8")
    assert "_safe_db_info" not in source
    assert "_safe_vector_info" not in source

    status = get_native_v2_data_status(
        save_dir=str(tmp_path / "downloaded"),
        data_root=str(tmp_path),
    )

    assert status["retrieval"]["mode"] == "unavailable"
    assert status["db"]["total_reports"] == 0
    assert status["vector_db"]["ntotal"] == 0


def test_format_duration_uses_compact_operator_units():
    assert format_duration(59) == "59초"
    assert format_duration(60) == "1분"
    assert format_duration(3600) == "1시간"
    assert format_duration(90000) == "1일 1시간"


def test_pending_cleanup_summary_tolerates_concurrent_file_disappearance(
    tmp_path,
    monkeypatch,
):
    catalog, _base_rows = _create_catalog(tmp_path)
    segment_id = _digest("disappearing-cleanup-segment")
    relative_path = f"deltas/{segment_id}.faiss"
    artifact_path = tmp_path / relative_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"pending cleanup")
    with sqlite3.connect(catalog) as connection:
        install_delta_schema(connection)
        connection.execute(
            """
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal, state, state_changed_at
            ) VALUES (?, 'snapshot-1', 7, 1, ?, ?, 15, 2, 'l2', 1,
                      'compacted', '2026-08-01T00:00:00.000Z')
            """,
            (segment_id, relative_path, _digest("pending cleanup")),
        )
        connection.commit()

    real_lstat = Path.lstat

    def disappear_before_stat(path: Path):
        if path == artifact_path:
            artifact_path.unlink()
            raise FileNotFoundError(str(artifact_path))
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", disappear_before_stat)
    with sqlite3.connect(catalog) as connection:
        connection.row_factory = sqlite3.Row
        summary = status_module._pending_cleanup_summary(
            connection,
            tmp_path,
        )

    assert summary == {
        "pending_cleanup_file_count": 0,
        "pending_cleanup_size_bytes": 0,
        "oldest_pending_cleanup_at": None,
        "oldest_pending_cleanup_age_seconds": 0,
    }


def test_native_status_includes_active_delta_membership_and_artifacts(
    tmp_path,
    monkeypatch,
):
    catalog, base_rows = _create_catalog(tmp_path)
    _publish_delta(
        catalog,
        tmp_path,
        sequence=1,
        changes=(
            _DeltaChange(
                "upsert",
                str(base_rows[0]["path"]),
                "replacement body",
                (9.0, 1.0),
            ),
            _DeltaChange("delete", str(base_rows[1]["path"])),
        ),
    )
    cleanup_payload = b"compacted artifact pending cleanup"
    cleanup_segment_id = _digest("compacted-status-segment")
    cleanup_relative_path = f"deltas/{cleanup_segment_id}.faiss"
    cleanup_path = tmp_path / cleanup_relative_path
    cleanup_path.write_bytes(cleanup_payload)
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            """
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal, state, state_changed_at
            ) VALUES (?, 'snapshot-1', 7, 2, ?, ?, ?, 2, 'l2', 1,
                      'compacted', '2026-08-01T00:00:00.000Z')
            """,
            (
                cleanup_segment_id,
                cleanup_relative_path,
                _digest("compacted artifact pending cleanup"),
                len(cleanup_payload),
            ),
        )
        connection.commit()
    paths = SimpleNamespace(catalog=catalog, data_root=tmp_path)
    selection = SimpleNamespace(
        mode="native",
        is_native=True,
        publication_generation=7,
        write_epoch=1,
        active_build_id="build-1",
        active_snapshot_id="snapshot-1",
        predecessor_snapshot_id=None,
        degraded=False,
        write_enabled=True,
    )
    monkeypatch.setattr(
        status_module,
        "_status_retrieval_paths",
        lambda _data_root: (paths, tmp_path),
    )
    monkeypatch.setattr(
        status_module,
        "inspect_runtime",
        lambda _data_root, **_kwargs: selection,
    )

    db, vector_db, retrieval = status_module._safe_native_info("unused.db")

    assert db["parent_chunks"] == 4
    assert retrieval["membership_count"] == 4
    assert retrieval["delta_generation"] == 1
    assert retrieval["delta_segment_count"] == 1
    assert retrieval["pending_cleanup_file_count"] == 1
    assert retrieval["pending_cleanup_size_bytes"] == len(cleanup_payload)
    assert retrieval["oldest_pending_cleanup_at"] == "2026-08-01T00:00:00.000Z"
    assert retrieval["oldest_pending_cleanup_age_seconds"] >= 0
    assert vector_db["ntotal"] == 4
    assert vector_db["file_count"] == 2
    assert {entry["name"] for entry in vector_db["files"]} == {
        "snapshot-1.faiss",
        next((tmp_path / "deltas").iterdir()).name,
    }
    assert vector_db["total_size_bytes"] == sum(
        entry["size_bytes"] for entry in vector_db["files"]
    )


def test_native_status_uses_build_manifest_instead_of_recomputing_active_views(
    tmp_path,
    monkeypatch,
):
    base_report_uids = [_digest(f"report-{index}") for index in range(1, 6)]
    manifest = {
        "schema_version": 1,
        "counts": {
            "discovered": len(base_report_uids),
            "included": len(base_report_uids),
            "excluded": 0,
        },
        "exclusion_policy": {
            "version": "test-native",
            "reason_codes": [],
        },
        "reports": [
            {
                "report_uid": report_uid,
                "status": "included",
                "reason_code": "included",
            }
            for report_uid in base_report_uids
        ],
    }
    catalog, base_rows = _create_catalog(
        tmp_path,
        source_manifest_json=json.dumps(manifest, sort_keys=True),
    )

    _publish_delta(
        catalog,
        tmp_path,
        sequence=1,
        changes=(
            _DeltaChange(
                "upsert",
                str(base_rows[0]["path"]),
                "replacement body",
                (9.0, 1.0),
            ),
            _DeltaChange("delete", str(base_rows[1]["path"])),
        ),
    )
    unindexed_body = "unindexed child"
    unindexed_content = f"prefix::{unindexed_body}::suffix"
    unindexed_parent_uid = _digest("unindexed-parent")
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            """
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order, content,
                content_sha256
            ) VALUES (?, 3, 'profile-1', 99, ?, ?)
            """,
            (
                unindexed_parent_uid,
                unindexed_content,
                _digest(unindexed_content),
            ),
        )
        connection.execute(
            """
            INSERT INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order, span_start,
                span_end, embedding_text_sha256
            ) VALUES (?, ?, 'profile-1', 0, ?, ?, ?)
            """,
            (
                _digest("unindexed-chunk"),
                unindexed_parent_uid,
                len("prefix::"),
                len("prefix::") + len(unindexed_body),
                _digest(unindexed_body),
            ),
        )
        connection.commit()
        authoritative_membership_count, authoritative_parent_count = connection.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT parent.parent_uid)
            FROM active_vector_membership AS membership
            JOIN retrieval_chunks AS chunk
              ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
             AND parent.profile_id = chunk.profile_id
            """
        ).fetchone()
    paths = SimpleNamespace(catalog=catalog, data_root=tmp_path)
    selection = SimpleNamespace(
        mode="native",
        is_native=True,
        publication_generation=7,
        write_epoch=1,
        active_build_id="build-1",
        active_snapshot_id="snapshot-1",
        predecessor_snapshot_id=None,
        degraded=False,
        write_enabled=True,
    )
    monkeypatch.setattr(
        status_module,
        "_status_retrieval_paths",
        lambda _data_root: (paths, tmp_path),
    )
    monkeypatch.setattr(
        status_module,
        "inspect_runtime",
        lambda _data_root, **_kwargs: selection,
    )

    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracing_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(status_module.sqlite3, "connect", tracing_connect)

    db, vector_db, retrieval = status_module._safe_native_info("unused.db")

    assert db["total_reports"] == 5
    assert db["embedded_reports"] == 4
    assert db["pending_reports"] == 1
    assert authoritative_parent_count == 4
    assert authoritative_membership_count == 4
    assert db["parent_chunks"] == authoritative_parent_count
    assert retrieval["membership_count"] == authoritative_membership_count
    assert vector_db["ntotal"] == 4
    executed = "\n".join(statements).casefold()
    assert "from active_reports" not in executed
    assert "from active_vector_membership" not in executed


def test_list_unembedded_reports_includes_ready_delta_failures_without_active_duplicates(
    tmp_path,
    monkeypatch,
):
    catalog, base_rows = _create_catalog(tmp_path)
    failed_uid = _digest("delta-failed-report")
    active_uid = _digest("report-1")
    segment_id = _digest("failed-segment")
    with sqlite3.connect(catalog) as connection:
        install_delta_schema(connection)
        connection.execute(
            """
            INSERT INTO reports (
                report_id, report_uid, canonical_relative_path, source_sha256,
                retrieval_metadata_sha256, report_type, report_date,
                target_name, title, broker
            ) VALUES (6, ?, 'reports/failed.pdf', ?, ?, 'company',
                      '2026-08-02', 'Failed', 'Failed report', 'Broker A')
            """,
            (failed_uid, _digest("failed-source"), _digest("failed-metadata")),
        )
        connection.execute(
            """
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal
            ) VALUES (?, 'snapshot-1', 7, 1,
                      NULL, NULL, 0, 2, 'l2', 0)
            """,
            (segment_id,),
        )
        connection.executemany(
            """
            INSERT INTO retrieval_delta_reports (
                segment_id, canonical_relative_path, action, report_uid, reason_code
            ) VALUES (?, ?, 'failed', ?, 'source-extraction-failed')
            """,
            [
                (segment_id, "reports/failed.pdf", failed_uid),
                (segment_id, str(base_rows[0]["path"]), active_uid),
            ],
        )
        connection.execute(
            "UPDATE retrieval_delta_segments SET state = 'ready' "
            "WHERE segment_id = ?",
            (segment_id,),
        )
        connection.commit()
    paths = SimpleNamespace(catalog=catalog, data_root=tmp_path)
    monkeypatch.setattr(
        status_module,
        "_status_retrieval_paths",
        lambda _data_root: (paths, tmp_path),
    )
    monkeypatch.setattr(
        status_module,
        "inspect_runtime",
        lambda _data_root, **_kwargs: SimpleNamespace(mode="native"),
    )

    rows = list_unembedded_reports("unused.db")

    assert [row["file_name"] for row in rows] == ["failed.pdf"]
    assert rows[0]["embedding_extraction_engine"] == "test-extractor"
    assert rows[0]["embedding_last_error"].startswith("NativeSourceExtractionError")


def test_status_reports_missing_native_authority_as_unavailable(tmp_path):
    status = get_data_status(data_root=str(tmp_path))

    assert status["retrieval"]["mode"] == "unavailable"
    assert status["retrieval"]["write_enabled"] is False
    assert status["db"]["total_reports"] == 0
    assert list_unembedded_reports(str(tmp_path)) == []


def test_list_unembedded_reports_maps_native_manifest_failure_to_management_row(
    tmp_path,
    monkeypatch,
):
    catalog = tmp_path / "retrieval" / "v2" / "catalog.sqlite3"
    catalog.parent.mkdir(parents=True)
    failed_uid = "ab" * 32
    manifest = {
        "schema_version": 1,
        "counts": {"discovered": 1, "included": 0, "excluded": 1},
        "exclusion_policy": {
            "version": "native-full-corpus-v2",
            "reason_codes": ["source-extraction-failed"],
        },
        "reports": [
            {
                "report_uid": failed_uid,
                "status": "excluded",
                "reason_code": "source-extraction-failed",
            }
        ],
    }
    with sqlite3.connect(catalog) as connection:
        connection.executescript(
            """
            CREATE TABLE reports (
                report_id INTEGER PRIMARY KEY,
                report_uid TEXT NOT NULL,
                canonical_relative_path TEXT NOT NULL,
                report_date TEXT NOT NULL,
                report_type TEXT NOT NULL,
                target_name TEXT,
                title TEXT NOT NULL,
                broker TEXT NOT NULL
            );
            CREATE TABLE active_reports (
                report_uid TEXT PRIMARY KEY
            );
            CREATE TABLE embedding_profiles (
                profile_id TEXT PRIMARY KEY,
                extractor TEXT NOT NULL
            );
            CREATE TABLE retrieval_builds (
                build_id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                source_manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE retrieval_runtime (
                runtime_id INTEGER PRIMARY KEY,
                active_build_id TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                failed_uid,
                "downloaded/failed.pdf",
                "2026-07-23",
                "company",
                "큐리오시스",
                "FDA 현대화법 3.0",
                "미래에셋증권",
            ),
        )
        connection.execute(
            "INSERT INTO embedding_profiles VALUES (?, ?)",
            ("profile-1", "pymupdf|fallback=opendataloader"),
        )
        connection.execute(
            "INSERT INTO retrieval_builds VALUES (?, ?, ?, ?)",
            (
                "build-1",
                "profile-1",
                json.dumps(manifest),
                "2026-07-25T08:30:00.000Z",
            ),
        )
        connection.execute(
            "INSERT INTO retrieval_runtime VALUES (1, 'build-1')"
        )

    monkeypatch.setattr(
        status_module,
        "retrieval_paths",
        lambda _data_root: SimpleNamespace(catalog=catalog, data_root=tmp_path),
    )
    monkeypatch.setattr(
        status_module,
        "inspect_runtime",
        lambda _data_root, **_kwargs: SimpleNamespace(mode="native"),
    )

    rows = status_module.list_unembedded_reports("unused.db")

    assert rows == [
        {
            "id": 1,
            "report_date": "2026-07-23",
            "report_type": "company",
            "target_name": "큐리오시스",
            "title": "FDA 현대화법 3.0",
            "broker": "미래에셋증권",
            "canonical_relative_path": "downloaded/failed.pdf",
            "file_name": "failed.pdf",
            "embedding_extraction_engine": "pymupdf|fallback=opendataloader",
            "embedding_last_error": (
                "NativeSourceExtractionError: primary and fallback extraction failed"
            ),
            "embedding_last_attempt_at": "2026-07-25T08:30:00.000Z",
        }
    ]


def test_build_crawler_env_passes_selected_categories():
    env = build_crawler_env(
        "2026-06-03",
        "2026-06-03",
        base_env={},
        categories=["industry", "economy"],
    )

    assert env["CRAWLER_CATEGORIES"] == "industry,economy"


def test_missing_update_dates_by_category_requires_all_selected_categories():
    counts = {
        "2026-06-01": {"company": 3, "industry": 1},
        "2026-06-02": {"company": 2},
        "2026-06-03": {"industry": 1},
    }

    assert missing_update_dates_by_category(
        "2026-06-01",
        "2026-06-03",
        counts,
        ["company", "industry"],
        today=date(2026, 6, 3),
    ) == ["2026-06-02", "2026-06-03"]


def test_issue_report_store_writes_reports_to_debug_folder(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    monkeypatch.setattr(issue_report_store, "get_app_version", lambda: "0.5.0")
    long_content = "앞부분" + ("가" * 1500) + "끝부분"
    messages = [
        {
            "id": index,
            "role": "user" if index % 2 else "assistant",
            "created_at": f"2026-06-17T00:{index:02d}:00+00:00",
            "content": long_content if index == 1 else f"메시지 {index}",
            "metadata": {"rerank_info": [{"rank": index}]},
        }
        for index in range(1, 11)
    ]
    context = issue_report_store.build_issue_report_context(
        thread={"id": "thread-1", "name": "테스트 대화"},
        messages=messages,
        include_conversation=True,
    )

    report_result = issue_report_store.create_issue_report(
        "thread-1",
        "답변 품질 문제",
        "출처와 답변이 맞지 않습니다.",
        context,
    )

    report_files = list(debug_dir.glob("issue_report_*.txt"))
    assert len(report_files) == 1
    assert report_result["file_path"] == str(report_files[0])

    saved_report = report_files[0].read_text(encoding="utf-8")
    assert f"Report ID: {report_result['id']}" in saved_report
    assert "App Version: 0.5.0" in saved_report
    assert "Thread ID: thread-1" in saved_report
    assert "Category: 답변 품질 문제" in saved_report
    assert "출처와 답변이 맞지 않습니다." in saved_report
    assert "conversation_message_count: 10" in saved_report
    assert "앞부분" in saved_report
    assert "끝부분" in saved_report
    assert "--- Message 10 ---" in saved_report
    assert '"rerank_info"' in saved_report
    assert '"rank": 10' in saved_report
    assert "이메일 본문에 그대로 붙여넣어" in saved_report

    sidecar_json = report_files[0].with_suffix(".json")
    assert sidecar_json.exists()

    reports = issue_report_store.list_issue_reports("thread-1")
    assert reports[0]["id"] == report_result["id"]
    assert reports[0]["app_version"] == "0.5.0"
    assert reports[0]["file_path"].endswith(".txt")
    assert reports[0]["description"] == "출처와 답변이 맞지 않습니다."
    assert reports[0]["context"]["thread_name"] == "테스트 대화"
    assert reports[0]["context"]["app_version"] == "0.5.0"


def test_import_issue_report_text_persists_emailed_report_with_source(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    raw_text = """Finance LLM 문제 신고
====================
Report ID: emailed123
Created At (UTC): 2026-06-28T04:53:33+00:00
App Version: 0.4.0
Thread ID: remote-thread
Category: 답변 품질 문제

Description:
마지막 답변이 일부 리포트만 참고했습니다.

Context:
- submitted_from: streamlit_chat

Conversation Messages:
- 첨부된 대화 없음
"""

    imported = issue_report_store.import_issue_report_text(raw_text)

    assert imported["id"] == "emailed123"
    assert imported["source"] == "email_import"
    assert Path(imported["file_path"]).exists()
    reports = issue_report_store.list_issue_reports()
    assert reports[0]["id"] == "emailed123"
    assert reports[0]["thread_id"] == "remote-thread"
    assert reports[0]["category"] == "답변 품질 문제"
    assert reports[0]["app_version"] == "0.4.0"
    assert reports[0]["source"] == "email_import"
    assert reports[0]["description"] == "마지막 답변이 일부 리포트만 참고했습니다."


def test_issue_report_email_guidance_uses_support_address(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)

    report = issue_report_store.create_issue_report(
        "thread-1",
        "검색/출처 문제",
        "출처가 누락됐습니다.",
        {"app_version": "0.4.0"},
    )

    text = Path(report["file_path"]).read_text(encoding="utf-8")
    assert "btr0813@naver.com" in text
    assert "[Finance LLM Issue][v0.4.0]" in text


def test_import_issue_report_text_restores_conversation_messages(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    raw_text = """Finance LLM 문제 신고
====================
Report ID: emailed456
Created At (UTC): 2026-06-28T04:53:33+00:00
App Version: 0.4.0
Thread ID: remote-thread
Category: 답변 품질 문제

Description:
삼성전자 요약이 이상합니다.

Context:
- submitted_from: streamlit_chat
- conversation_message_count: 2

Conversation Messages:

--- Message 1 ---
ID: 1
Role: user
Created At: 2026-06-28T04:00:00+00:00
Metadata:
{}
Content:
올해 삼성전자 리포트 시기별로 요약해줘

--- Message 2 ---
ID: 2
Role: assistant
Created At: 2026-06-28T04:00:05+00:00
Metadata:
{
  "status": "succeeded",
  "route": "vectordb",
  "search_scope": {
    "search_filters": {"target_name": "삼성전자"},
    "file_names": ["samsung-a.pdf"]
  }
}
Content:
일부 리포트만 요약했습니다.
"""

    issue_report_store.import_issue_report_text(raw_text)

    report = issue_report_store.list_issue_reports()[0]
    messages = report["context"]["conversation_messages"]
    assert report["context"]["submitted_from"] == "email_import"
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "올해 삼성전자 리포트 시기별로 요약해줘"
    assert messages[1]["metadata"]["route"] == "vectordb"
    assert messages[1]["metadata"]["search_scope"]["search_filters"] == {"target_name": "삼성전자"}
