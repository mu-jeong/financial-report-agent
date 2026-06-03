import sqlite3

from src.core.status import assess_readiness, format_readiness_text, format_status_text, get_data_status


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
    assert status["db"]["total_reports"] == 2
    assert status["db"]["embedded_reports"] == 1
    assert status["db"]["pending_reports"] == 1
    assert status["db"]["parent_chunks"] == 1
    assert status["db"]["min_report_date"] == "2026-02-05"
    assert status["db"]["max_report_date"] == "2026-02-06"
    assert status["db"]["report_date_counts"] == {
        "2026-02-05": 1,
    }
    assert status["vector_db"]["has_faiss_index"] is True
    assert status["vector_db"]["total_size_bytes"] == 6
    assert "SQLite 리포트: 2건" in format_status_text(status)


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
