from pathlib import Path

from src.core import embed_pipeline
from src.core.pdf_extraction import ExtractionResult


def test_node_extract_pdf_uses_unembedded_extraction_engine_for_pending_docs(tmp_path, monkeypatch):
    captured = {}
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")

    def fake_extract_pdf_text(pdf_path, engine, *, clean=True, allow_fallback=True):
        captured["pdf_path"] = Path(pdf_path)
        captured["engine"] = engine
        captured["clean"] = clean
        captured["allow_fallback"] = allow_fallback
        return ExtractionResult(
            requested_engine=engine,
            used_engine=engine,
            text="대체 변환 엔진으로 추출된 본문입니다.",
        )

    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(pdf_dir))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(embed_pipeline.config, "UNEMBEDDED_EXTRACTION_ENGINE", "opendataloader", raising=False)
    monkeypatch.setattr(embed_pipeline, "extract_pdf_text", fake_extract_pdf_text)

    result = embed_pipeline.node_extract_pdf({"file_name": "report.pdf"})

    assert captured == {
        "pdf_path": pdf_dir / "report.pdf",
        "engine": "opendataloader",
        "clean": True,
        "allow_fallback": False,
    }
    assert result["raw_text"] == "대체 변환 엔진으로 추출된 본문입니다."
    assert result["extraction_engine"] == "opendataloader"


def test_node_extract_pdf_falls_back_to_default_engine_when_unembedded_engine_is_empty(tmp_path, monkeypatch):
    captured = {}
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")

    def fake_extract_pdf_text(pdf_path, engine, *, clean=True, allow_fallback=True):
        captured["engine"] = engine
        captured["allow_fallback"] = allow_fallback
        return ExtractionResult(
            requested_engine=engine,
            used_engine=engine,
            text="기본 변환 엔진으로 추출된 본문입니다.",
        )

    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(pdf_dir))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(embed_pipeline.config, "UNEMBEDDED_EXTRACTION_ENGINE", "", raising=False)
    monkeypatch.setattr(embed_pipeline, "extract_pdf_text", fake_extract_pdf_text)

    result = embed_pipeline.node_extract_pdf({"file_name": "report.pdf"})

    assert captured["engine"] == "pymupdf"
    assert captured["allow_fallback"] is True
    assert result["extraction_engine"] == "pymupdf"


def test_run_pipeline_records_extraction_engine_on_failed_pending_document(tmp_path, monkeypatch):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")
    captured = {}

    row = {
        "file_name": "report.pdf",
        "target_name": "SK하이닉스",
        "title": "DRAM 전망",
        "report_date": "2026-06-27",
        "report_type": "company",
        "broker": "테스트증권",
    }

    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(pdf_dir))
    monkeypatch.setattr(embed_pipeline.config, "FAISS_DIR", str(tmp_path / "vector_db"))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(embed_pipeline.config, "UNEMBEDDED_EXTRACTION_ENGINE", "opendataloader", raising=False)
    monkeypatch.setattr(embed_pipeline, "init_db", lambda: None)
    monkeypatch.setattr(embed_pipeline, "sync_report_pdf_dir_env", lambda _pdf_dir: None)
    monkeypatch.setattr(embed_pipeline, "sync_from_directory", lambda _pdf_dir: None)
    monkeypatch.setattr(embed_pipeline, "fetch_unembedded", lambda: [row])
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", lambda: object())
    monkeypatch.setattr(embed_pipeline.time, "sleep", lambda _seconds: None)

    def fail_extract_pdf(state):
        raise ValueError("broken parser")

    def fake_mark_embedding_failed(file_name, error_message, *, extraction_engine=None):
        captured["file_name"] = file_name
        captured["error_message"] = error_message
        captured["extraction_engine"] = extraction_engine

    monkeypatch.setattr(embed_pipeline, "node_extract_pdf", fail_extract_pdf)
    monkeypatch.setattr(embed_pipeline, "mark_embedding_failed", fake_mark_embedding_failed)

    exit_code = embed_pipeline.run_pipeline(test_limit=1)

    assert exit_code == 1
    assert captured == {
        "file_name": "report.pdf",
        "error_message": "ValueError: broken parser",
        "extraction_engine": "opendataloader",
    }
