from pathlib import Path
from types import SimpleNamespace

from src.core import embed_pipeline
from src.core.pdf_extraction import ExtractionResult
from src.retrieval import build_service


def test_run_pipeline_holds_cutover_fence_for_the_whole_update(
    tmp_path,
    monkeypatch,
):
    events = []
    data_root = tmp_path / "data"
    data_root.mkdir()

    class FakeUpdateLock:
        def __init__(self, observed_root):
            assert Path(observed_root) == data_root

        def __enter__(self):
            events.append("locked")
            return self

        def __exit__(self, *_args):
            events.append("unlocked")

    def run_locked(limit):
        assert events == ["locked"]
        events.append(f"pipeline:{limit}")
        return 0

    monkeypatch.setattr(embed_pipeline.config, "DB_PATH", str(data_root / "reports.db"))
    monkeypatch.setattr(embed_pipeline, "RetrievalUpdateLock", FakeUpdateLock)
    monkeypatch.setattr(embed_pipeline, "_run_pipeline_locked", run_locked)

    assert embed_pipeline.run_pipeline(test_limit=7) == 0
    assert events == ["locked", "pipeline:7", "unlocked"]


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


def test_node_extract_pdf_keeps_v1_requested_engine_after_successful_fallback(
    tmp_path,
    monkeypatch,
):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")
    captured = {}

    def fake_extract_pdf_text(pdf_path, engine, *, clean=True, allow_fallback=True):
        captured["engine"] = engine
        captured["allow_fallback"] = allow_fallback
        return ExtractionResult(
            requested_engine=engine,
            used_engine="pymupdf-fallback",
            text="fallback candidate text",
        )

    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(pdf_dir))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "opendataloader")
    monkeypatch.setattr(
        embed_pipeline.config,
        "UNEMBEDDED_EXTRACTION_ENGINE",
        "",
        raising=False,
    )
    monkeypatch.setattr(embed_pipeline, "extract_pdf_text", fake_extract_pdf_text)

    result = embed_pipeline.node_extract_pdf({"file_name": "report.pdf"})

    assert captured == {
        "engine": "opendataloader",
        "allow_fallback": True,
    }
    assert result["raw_text"] == "fallback candidate text"
    assert result["extraction_engine"] == "opendataloader"


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
    monkeypatch.setattr(
        embed_pipeline,
        "guard_before_retrieval_write",
        lambda _path, **_kwargs: SimpleNamespace(is_native=False),
    )
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


def test_run_pipeline_routes_native_runtime_to_incremental_update(
    tmp_path,
    monkeypatch,
):
    calls = []
    data_root = tmp_path / "native root"
    source_root = tmp_path / "pdfs"
    data_root.mkdir()
    source_root.mkdir()
    runtime = SimpleNamespace(
        is_native=True,
        paths=SimpleNamespace(data_root=data_root),
    )
    embeddings = object()
    result = SimpleNamespace(report_count=4, chunk_count=12)
    outcome = SimpleNamespace(publication_generation=2, write_epoch=1)

    monkeypatch.setattr(
        embed_pipeline,
        "guard_before_retrieval_write",
        lambda _path, **_kwargs: runtime,
    )
    monkeypatch.setattr(embed_pipeline.config, "DB_PATH", str(data_root / "reports.db"))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(
        embed_pipeline.config,
        "UNEMBEDDED_EXTRACTION_ENGINE",
        "opendataloader",
        raising=False,
    )
    monkeypatch.setattr(embed_pipeline.config, "USE_PARENT_CHILD", False)
    monkeypatch.setattr(embed_pipeline.config, "CHUNK_SIZE", 777)
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", lambda: embeddings)
    monkeypatch.setattr(
        embed_pipeline,
        "init_db",
        lambda: (_ for _ in ()).throw(AssertionError("legacy DB init is unreachable")),
    )

    def fake_execute(db_path, source_directory, **kwargs):
        calls.append((db_path, source_directory, kwargs))
        return result, outcome

    monkeypatch.setattr(build_service, "execute_incremental_update", fake_execute)

    assert embed_pipeline.run_pipeline(test_limit=1) == 0
    assert len(calls) == 1
    assert calls[0][0] == str(data_root / "reports.db")
    assert calls[0][1] == str(source_root)
    assert calls[0][2]["data_root"] == data_root
    assert calls[0][2]["embeddings"] is embeddings
    assert calls[0][2]["extractor_name"] == "opendataloader"
    assert calls[0][2]["allow_extraction_fallback"] is False
    assert calls[0][2]["use_parent_child"] is False
    assert calls[0][2]["single_chunk_size"] == 777


def test_run_pipeline_native_runtime_keeps_default_extractor_fallback_policy(
    tmp_path,
    monkeypatch,
):
    captured = {}
    data_root = tmp_path / "native"
    source_root = tmp_path / "pdfs"
    data_root.mkdir()
    source_root.mkdir()
    runtime = SimpleNamespace(
        is_native=True,
        paths=SimpleNamespace(data_root=data_root),
    )

    monkeypatch.setattr(
        embed_pipeline,
        "guard_before_retrieval_write",
        lambda _path, **_kwargs: runtime,
    )
    monkeypatch.setattr(embed_pipeline.config, "DB_PATH", str(data_root / "reports.db"))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "opendataloader")
    monkeypatch.setattr(
        embed_pipeline.config,
        "UNEMBEDDED_EXTRACTION_ENGINE",
        "",
        raising=False,
    )
    monkeypatch.setattr(embed_pipeline.config, "USE_PARENT_CHILD", True)
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", object)

    def fake_execute(_db_path, _source_directory, **kwargs):
        captured.update(kwargs)
        return (
            SimpleNamespace(report_count=1, chunk_count=1),
            SimpleNamespace(publication_generation=2, write_epoch=1),
        )

    monkeypatch.setattr(build_service, "execute_incremental_update", fake_execute)

    assert embed_pipeline.run_pipeline() == 0
    assert captured["extractor_name"] == "opendataloader"
    assert captured["allow_extraction_fallback"] is True
    assert captured["use_parent_child"] is True
