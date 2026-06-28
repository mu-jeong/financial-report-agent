from datetime import date
import sqlite3

from src.core import issue_report_store
from src.core.data_update_jobs import build_crawler_env, missing_update_dates_by_category
from src.core.status import (
    assess_readiness,
    build_unembedded_report_rows,
    format_readiness_text,
    format_status_text,
    get_data_status,
    list_unembedded_reports,
)


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
    assert '"rerank_count": 1' in saved_report
    assert "파일 내용을 복사하여 이메일의 내용에 첨부" in saved_report

    sidecar_json = report_files[0].with_suffix(".json")
    assert sidecar_json.exists()

    reports = issue_report_store.list_issue_reports("thread-1")
    assert reports[0]["id"] == report_result["id"]
    assert reports[0]["app_version"] == "0.5.0"
    assert reports[0]["file_path"].endswith(".txt")
    assert reports[0]["description"] == "출처와 답변이 맞지 않습니다."
    assert reports[0]["context"]["thread_name"] == "테스트 대화"
    assert reports[0]["context"]["app_version"] == "0.5.0"
