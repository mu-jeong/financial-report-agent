import sqlite3

from src.core.status import format_status_text, get_data_status


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
                ('company', '2026-02-06', 'SK하이닉스', 'DRAM', '하나증권', 'b.pdf', 0)
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
    assert status["vector_db"]["has_faiss_index"] is True
    assert status["vector_db"]["total_size_bytes"] == 6
    assert "SQLite 리포트: 2건" in format_status_text(status)
