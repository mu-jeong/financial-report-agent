from datetime import date
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from src.core import issue_report_store, status as status_module
from src.core.data_update_jobs import build_crawler_env, missing_update_dates_by_category
from src.core.status import (
    assess_readiness,
    build_unembedded_report_rows,
    format_readiness_text,
    format_status_text,
    get_data_status,
    list_unembedded_reports,
)
from src.migrations.v2.evidence import seal_compatibility_bundle
from src.migrations.v2.import_v1 import convert_v1_seed
from tests.migrations.v2.fixtures_factory.v1 import build_v1_fixture


def test_get_data_status_reports_db_pdf_and_vector_counts(tmp_path):
    save_dir = tmp_path / "downloaded"
    save_dir.mkdir()
    (save_dir / "a.pdf").write_bytes(b"%PDF")
    (save_dir / "ignore.txt").write_text("x")

    faiss_dir = tmp_path / "vector_db"
    faiss_dir.mkdir()
    (faiss_dir / "index.faiss").write_bytes(b"1234")
    (faiss_dir / "index.pkl").write_bytes(b"12")

    db_path = tmp_path / "reports.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type TEXT NOT NULL,
                report_date TEXT NOT NULL,
                target_name TEXT,
                title TEXT NOT NULL,
                broker TEXT NOT NULL,
                file_name TEXT NOT NULL UNIQUE,
                is_embedded INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE TABLE parent_chunks (id TEXT PRIMARY KEY, content TEXT, file_name TEXT, metadata TEXT)")
        conn.execute(
            """
            INSERT INTO reports
                (report_type, report_date, target_name, title, broker, file_name, is_embedded)
            VALUES
                ('company', '2026-02-05', '삼성전자', 'HBM', '미래에셋증권', 'a.pdf', 1),
                ('industry', '2026-02-05', '반도체', '업황', '미래에셋증권', 'c.pdf', 1),
                ('company', ' 2026-02-06 12:34:56 ', 'SK하이닉스', 'DRAM', '하나증권', 'b.pdf', 0)
            """
        )
        conn.execute("INSERT INTO parent_chunks VALUES ('p1', 'content', 'a.pdf', '{}')")

    status = get_data_status(
        save_dir=str(save_dir),
        db_path=str(db_path),
        faiss_dir=str(faiss_dir),
    )

    assert status["downloaded_pdfs"] == 1
    assert status["db"]["total_reports"] == 3
    assert status["db"]["embedded_reports"] == 2
    assert status["db"]["pending_reports"] == 1
    assert status["db"]["parent_chunks"] == 1
    assert status["db"]["min_report_date"] == "2026-02-05"
    assert status["db"]["max_report_date"] == "2026-02-06"
    assert status["db"]["report_date_counts"] == {
        "2026-02-05": 2,
    }
    assert status["db"]["report_date_type_counts"] == {
        "2026-02-05": {
            "company": 1,
            "industry": 1,
        },
    }
    assert status["vector_db"]["has_faiss_index"] is True
    assert status["vector_db"]["total_size_bytes"] == 6
    assert "SQLite 리포트: 3건" in format_status_text(status)


def test_get_data_status_uses_native_membership_without_pickle_assumptions(tmp_path):
    copied = tmp_path / "copied"
    fixture = build_v1_fixture(copied)
    data_root = tmp_path / "native data 한글"
    bundle = seal_compatibility_bundle(copied, data_root)
    result = convert_v1_seed(
        copied,
        data_root,
        expected_hashes=fixture.artifact_hashes,
        profile=fixture.embedding_profile(),
        source_hashes=fixture.source_hashes,
        compatibility_bundle_id=bundle.bundle_id,
    )

    status = get_data_status(
        save_dir=str(data_root / "downloaded"),
        db_path=str(data_root / "reports.db"),
        faiss_dir=str(data_root / "legacy-vector-db"),
    )

    assert status["retrieval"]["mode"] == "native"
    assert status["retrieval"]["write_epoch"] == 0
    assert status["retrieval"]["membership_count"] == fixture.symbolic_n
    assert status["vector_db"]["ntotal"] == fixture.symbolic_n
    assert status["vector_db"]["has_faiss_index"] is True
    assert status["vector_db"]["has_pickle_index"] is False
    assert status["db"]["total_reports"] == 4
    assert status["db"]["embedded_reports"] == 3
    assert status["db"]["pending_reports"] == 1
    assert Path(status["paths"]["db_path"]) == (
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    pending_rows = list_unembedded_reports(status["paths"]["db_path"])
    assert [row["file_name"] for row in pending_rows] == [
        fixture.file_names["excluded"]
    ]
    assert result.snapshot_id == status["retrieval"]["active_snapshot_id"]


def test_status_never_downgrades_missing_native_authority_to_legacy(tmp_path):
    db_path = tmp_path / "reports.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY,
                report_type TEXT,
                report_date TEXT,
                target_name TEXT,
                title TEXT,
                broker TEXT,
                file_name TEXT,
                is_embedded INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO reports VALUES (1, 'company', '2026-07-16', "
            "'stale', 'stale', 'stale', 'stale.pdf', 0)"
        )
    evidence = tmp_path / "retrieval" / "v2" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "native-authority.marker").write_text("present", encoding="utf-8")

    status = get_data_status(
        save_dir=str(tmp_path / "downloaded"),
        db_path=str(db_path),
        faiss_dir=str(tmp_path / "vector_db"),
    )

    assert status["retrieval"]["mode"] == "unavailable"
    assert status["retrieval"]["write_enabled"] is False
    assert "V2 recovery evidence" in status["retrieval"]["error"]
    assert status["db"]["total_reports"] == 0
    assert list_unembedded_reports(str(db_path)) == []


def test_list_unembedded_reports_returns_recent_pending_rows_and_safe_previews(tmp_path):
    db_path = tmp_path / "reports.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_type TEXT NOT NULL,
                report_date TEXT NOT NULL,
                target_name TEXT,
                title TEXT NOT NULL,
                broker TEXT NOT NULL,
                file_name TEXT NOT NULL UNIQUE,
                is_embedded INTEGER NOT NULL DEFAULT 0,
                embedding_last_error TEXT,
                embedding_last_attempt_at TEXT,
                embedding_extraction_engine TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO reports
                (report_type, report_date, target_name, title, broker, file_name, is_embedded, embedding_last_error, embedding_last_attempt_at, embedding_extraction_engine)
            VALUES
                ('company', '2026-06-20', '삼성전자', '이미 처리됨', '미래에셋증권', 'embedded.pdf', 1, NULL, NULL, 'pymupdf'),
                ('company', '2026-06-22', 'SK하이닉스', '2Q26 영업이익 전망', 'iM증권', 'sk_a.pdf', 0, NULL, NULL, NULL),
                ('industry', '2026-06-23', '반도체', '업황 업데이트', '하나증권', 'semi.pdf', 0, 'FileNotFoundError: PDF missing', '2026-06-27T10:00:00', 'opendataloader'),
                ('company', '2026-06-24', 'SK하이닉스', 'ADR 발행 관련 리포트', 'IBK투자증권', 'sk_b.pdf', 0, NULL, NULL, NULL)
            """
        )

    rows = list_unembedded_reports(str(db_path), limit=2)
    table_rows = build_unembedded_report_rows(rows)

    assert [row["file_name"] for row in rows] == ["sk_b.pdf", "semi.pdf"]
    assert table_rows[0] == {
        "report_date": "2026-06-24",
        "report_type": "company",
        "target_name": "SK하이닉스",
        "broker": "IBK투자증권",
        "title": "ADR 발행 관련 리포트",
        "file_name": "sk_b.pdf",
        "embedding_extraction_engine": "-",
        "embedding_last_error": "-",
        "embedding_last_attempt_at": "-",
    }
    assert table_rows[1]["embedding_extraction_engine"] == "opendataloader"
    assert table_rows[1]["embedding_last_error"] == "FileNotFoundError: PDF missing"
    assert table_rows[1]["embedding_last_attempt_at"] == "2026-06-27T10:00:00"


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
        lambda _db_path: SimpleNamespace(catalog=catalog),
    )
    monkeypatch.setattr(
        status_module,
        "inspect_runtime",
        lambda _db_path, **_kwargs: SimpleNamespace(mode="native"),
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


def test_assess_readiness_blocks_when_index_is_missing():
    status = {
        "downloaded_pdfs": 0,
        "db": {
            "exists": True,
            "error": None,
            "total_reports": 2,
            "embedded_reports": 0,
            "pending_reports": 2,
            "min_report_date": "2026-06-01",
            "max_report_date": "2026-06-02",
        },
        "vector_db": {
            "exists": False,
            "has_faiss_index": False,
            "has_pickle_index": False,
            "file_count": 0,
            "total_size_bytes": 0,
        },
        "embedding_limit_active": False,
        "search_coverage_ratio": 0.0,
        "config": {},
        "paths": {},
    }

    readiness = assess_readiness(status)

    assert readiness["level"] == "blocked"
    assert readiness["label"] == "준비 필요"
    assert any("FAISS 검색 인덱스" in message for message in readiness["messages"])
    assert "Quick Start 준비 상태: 준비 필요" in format_readiness_text(status)


def test_assess_readiness_warns_for_partial_embedding_and_pickle_index():
    status = {
        "downloaded_pdfs": 2,
        "db": {
            "exists": True,
            "error": None,
            "total_reports": 2,
            "embedded_reports": 1,
            "pending_reports": 1,
            "min_report_date": "2026-06-01",
            "max_report_date": "2026-06-02",
        },
        "vector_db": {
            "exists": True,
            "has_faiss_index": True,
            "has_pickle_index": True,
            "file_count": 2,
            "total_size_bytes": 16,
        },
        "embedding_limit_active": True,
        "search_coverage_ratio": 0.5,
        "config": {},
        "paths": {},
    }

    readiness = assess_readiness(status)

    assert readiness["level"] == "warning"
    assert readiness["label"] == "주의 필요"
    assert any("임베딩되지 않은 리포트" in message for message in readiness["messages"])
    assert any("pickle 기반" in message for message in readiness["messages"])


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
