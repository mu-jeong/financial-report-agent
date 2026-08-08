from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import embed_pipeline, pdf_extraction
from src.core.pdf_extraction import ExtractionResult
from src.retrieval import build_service, continuous_update
from src.retrieval.bootstrap import (
    inspect_runtime,
    reconcile_and_inspect_runtime as real_reconcile_and_inspect_runtime,
)


@pytest.fixture(autouse=True)
def _isolate_runtime_reconciliation(monkeypatch):
    """Greenfield reconciliation is covered by retrieval integration tests."""

    monkeypatch.setattr(
        embed_pipeline,
        "reconcile_and_inspect_runtime",
        lambda _root: None,
    )


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

    def run_locked(*, retry_extraction_failures=False):
        assert events == ["locked"]
        assert retry_extraction_failures is False
        events.append("pipeline")
        return 0

    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(embed_pipeline, "RetrievalUpdateLock", FakeUpdateLock)
    monkeypatch.setattr(embed_pipeline, "_run_pipeline_locked", run_locked)

    assert embed_pipeline.run_pipeline() == 0
    assert events == ["locked", "pipeline", "unlocked"]


def test_main_reconciles_greenfield_root_to_exact_empty_without_embeddings(
    tmp_path,
    monkeypatch,
):
    data_root = tmp_path / "data"
    source_root = tmp_path / "pdfs"
    data_root.mkdir()
    source_root.mkdir()

    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(
        embed_pipeline,
        "reconcile_and_inspect_runtime",
        real_reconcile_and_inspect_runtime,
    )
    monkeypatch.setattr(
        embed_pipeline,
        "build_embeddings_fn",
        lambda: pytest.fail("empty update initialized embeddings"),
    )

    with pytest.raises(SystemExit) as exited:
        embed_pipeline.main([])
    assert exited.value.code == 0

    runtime = inspect_runtime(data_root)
    assert runtime.mode == "native"
    assert runtime.initialization_state == "empty"
    assert (
        runtime.active_snapshot_id,
        runtime.active_build_id,
        runtime.predecessor_snapshot_id,
        runtime.publication_generation,
        runtime.write_epoch,
        runtime.degraded,
        runtime.write_enabled,
    ) == (None, None, None, 0, 0, False, False)


def test_node_extract_pdf_uses_unembedded_extraction_engine_for_pending_docs(tmp_path, monkeypatch):
    captured = {}
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")

    def fake_extract_pdf_text(
        pdf_path,
        engine,
        *,
        clean=True,
        allow_fallback=True,
        fallback_engine=None,
    ):
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

    def fake_extract_pdf_text(
        pdf_path,
        engine,
        *,
        clean=True,
        allow_fallback=True,
        fallback_engine=None,
    ):
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


def test_node_extract_pdf_keeps_requested_engine_after_successful_fallback(
    tmp_path,
    monkeypatch,
):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")
    captured = {}

    def fake_extract_pdf_text(
        pdf_path,
        engine,
        *,
        clean=True,
        allow_fallback=True,
        fallback_engine=None,
    ):
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


@pytest.mark.parametrize(
    ("primary_engine", "fallback_engine"),
    [
        ("unsupported-primary", "opendataloader"),
        ("pymupdf", "unsupported-fallback"),
    ],
)
def test_node_extract_pdf_rejects_invalid_engine_policy_before_extraction(
    tmp_path,
    monkeypatch,
    primary_engine,
    fallback_engine,
):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")

    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(pdf_dir))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", primary_engine)
    monkeypatch.setattr(
        embed_pipeline.config,
        "UNEMBEDDED_EXTRACTION_ENGINE",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        embed_pipeline.config,
        "EXTRACTION_FALLBACK_ENGINE",
        fallback_engine,
        raising=False,
    )
    monkeypatch.setattr(
        embed_pipeline,
        "extract_pdf_text",
        lambda *_args, **_kwargs: pytest.fail("invalid policy reached extraction"),
    )

    with pytest.raises(ValueError, match="Unsupported extraction engine"):
        embed_pipeline.node_extract_pdf({"file_name": "report.pdf"})


def test_node_extract_pdf_preserves_primary_and_fallback_failures(
    tmp_path,
    monkeypatch,
):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "report.pdf").write_bytes(b"%PDF")

    def fail_both(_path, engine):
        raise RuntimeError(f"{engine} parser failed")

    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(pdf_dir))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(
        embed_pipeline.config,
        "UNEMBEDDED_EXTRACTION_ENGINE",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        embed_pipeline.config,
        "EXTRACTION_FALLBACK_ENGINE",
        "opendataloader",
        raising=False,
    )
    monkeypatch.setattr(pdf_extraction, "_extract_pdf_text", fail_both)

    with pytest.raises(embed_pipeline.PdfExtractionError) as raised:
        embed_pipeline.node_extract_pdf({"file_name": "report.pdf"})

    message = str(raised.value)
    assert "pymupdf parser failed" in message
    assert "opendataloader parser failed" in message


def test_run_pipeline_rejects_non_native_runtime(monkeypatch):
    monkeypatch.setattr(
        embed_pipeline,
        "guard_before_retrieval_write",
        lambda _path, **_kwargs: SimpleNamespace(is_native=False),
    )

    with pytest.raises(RuntimeError, match="Native V2 retrieval runtime is required"):
        embed_pipeline._run_pipeline_locked()


def test_native_source_extraction_error_cannot_be_hidden_as_quickstart_success(
    tmp_path,
    monkeypatch,
):
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
    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(embed_pipeline.config, "USE_PARENT_CHILD", True)
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", object)

    def fail_update(*_args, **_kwargs):
        raise build_service.NativeSourceExtractionError(
            "source extraction failed for report.pdf"
        )

    monkeypatch.setattr(continuous_update, "execute_continuous_update", fail_update)

    assert embed_pipeline.run_pipeline() == 1


def test_quickstart_mode_does_not_hide_native_profile_mismatch(
    tmp_path,
    monkeypatch,
):
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
    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(embed_pipeline.config, "USE_PARENT_CHILD", True)
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", object)

    def reject_profile(*_args, **_kwargs):
        raise build_service.NativeBuildError(
            "incremental extractor differs from the active embedding profile"
        )

    monkeypatch.setattr(continuous_update, "execute_continuous_update", reject_profile)

    assert embed_pipeline.run_pipeline() == 1


def test_run_pipeline_routes_native_runtime_to_continuous_update(
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
    candidate_result = SimpleNamespace(
        report_count=4,
        chunk_count=12,
    )
    outcome = SimpleNamespace(publication_generation=2, write_epoch=1)
    completed = SimpleNamespace(
        delta_publications=(),
        candidate_result=candidate_result,
        publication_outcome=outcome,
        attempted_report_uids=("report-1",),
        failed_report_uids=(),
    )

    monkeypatch.setattr(
        embed_pipeline,
        "guard_before_retrieval_write",
        lambda _path, **_kwargs: runtime,
    )
    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(data_root))
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

    def fake_execute(data_root_arg, source_directory, **kwargs):
        calls.append((data_root_arg, source_directory, kwargs))
        return completed

    monkeypatch.setattr(continuous_update, "execute_continuous_update", fake_execute)

    assert embed_pipeline.run_pipeline(
        retry_extraction_failures=True,
    ) == 0
    assert len(calls) == 1
    assert calls[0][0] == str(data_root)
    assert calls[0][1] == str(source_root)
    assert calls[0][2]["embeddings"] is embeddings
    assert calls[0][2]["extractor_name"] == "opendataloader"
    assert calls[0][2]["allow_extraction_fallback"] is False
    assert calls[0][2]["retry_extraction_failures"] is True
    assert calls[0][2]["use_parent_child"] is False
    assert calls[0][2]["single_chunk_size"] == 777
    assert (
        calls[0][2]["batch_size"]
        == continuous_update.DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE
    )
    assert callable(calls[0][2]["progress_callback"])
    assert "max_changed_reports" not in calls[0][2]
    assert "skip_report_uids" not in calls[0][2]


def test_run_pipeline_reports_delta_batches_then_one_final_compaction(
    tmp_path,
    monkeypatch,
    caplog,
):
    calls = []
    batch_size = continuous_update.DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE
    total_reports = batch_size * 2 + batch_size // 2
    data_root = tmp_path / "native"
    source_root = tmp_path / "pdfs"
    data_root.mkdir()
    source_root.mkdir()
    runtime = SimpleNamespace(
        is_native=True,
        paths=SimpleNamespace(data_root=data_root),
    )
    embeddings = object()
    deltas = [
        SimpleNamespace(
            sequence=2,
            attempted_report_uids=tuple(
                f"report-{index}" for index in range(batch_size)
            ),
            published_report_uids=tuple(
                f"report-{index}" for index in range(batch_size)
            ),
            failed_report_uids=(),
            deferred_report_count=total_reports - batch_size,
        ),
        SimpleNamespace(
            sequence=3,
            attempted_report_uids=tuple(
                f"report-{index}" for index in range(batch_size, batch_size * 2)
            ),
            published_report_uids=tuple(
                f"report-{index}" for index in range(batch_size, batch_size * 2)
            ),
            failed_report_uids=(),
            deferred_report_count=total_reports - batch_size * 2,
        ),
        SimpleNamespace(
            sequence=4,
            attempted_report_uids=tuple(
                f"report-{index}" for index in range(batch_size * 2, total_reports)
            ),
            published_report_uids=tuple(
                f"report-{index}" for index in range(batch_size * 2, total_reports)
            ),
            failed_report_uids=(),
            deferred_report_count=0,
        ),
    ]

    monkeypatch.setattr(
        embed_pipeline,
        "guard_before_retrieval_write",
        lambda _path, **_kwargs: runtime,
    )
    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(embed_pipeline.config, "USE_PARENT_CHILD", True)
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", lambda: embeddings)

    def fake_execute(_data_root, _source_directory, **kwargs):
        calls.append(kwargs)
        for delta in deltas:
            kwargs["progress_callback"](delta)
        return SimpleNamespace(
            delta_publications=tuple(deltas),
            candidate_result=SimpleNamespace(
                report_count=total_reports,
                chunk_count=total_reports * 3,
            ),
            publication_outcome=SimpleNamespace(
                publication_generation=5,
                write_epoch=4,
            ),
            attempted_report_uids=tuple(
                f"report-{index}" for index in range(total_reports)
            ),
            failed_report_uids=(),
        )

    monkeypatch.setattr(continuous_update, "execute_continuous_update", fake_execute)

    caplog.set_level("INFO", logger="src.core.embed_pipeline")
    assert embed_pipeline.run_pipeline() == 0
    assert len(calls) == 1
    assert all(call["embeddings"] is embeddings for call in calls)
    assert calls[0]["batch_size"] == batch_size
    assert "max_changed_reports" not in calls[0]
    assert "skip_report_uids" not in calls[0]
    delta_messages = [
        record.message
        for record in caplog.records
        if "Native V2 delta publication complete" in record.message
    ]
    assert len(delta_messages) == 3
    assert f"processed={batch_size}" in delta_messages[0]
    assert f"processed={batch_size * 2}" in delta_messages[1]
    assert f"processed={total_reports}" in delta_messages[2]
    final_compaction_messages = [
        record.message
        for record in caplog.records
        if "Native V2 final compaction complete" in record.message
    ]
    assert len(final_compaction_messages) == 1
    compaction_messages = [
        record.message
        for record in caplog.records
        if "Native V2 update complete" in record.message
    ]
    assert len(compaction_messages) == 1
    assert "deltas=3 compactions=1" in compaction_messages[0]


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
    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(embed_pipeline.config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(
        embed_pipeline.config,
        "EXTRACTION_FALLBACK_ENGINE",
        "opendataloader",
        raising=False,
    )
    monkeypatch.setattr(
        embed_pipeline.config,
        "UNEMBEDDED_EXTRACTION_ENGINE",
        "",
        raising=False,
    )
    monkeypatch.setattr(embed_pipeline.config, "USE_PARENT_CHILD", True)
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", object)

    def fake_execute(_data_root, _source_directory, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            delta_publications=(),
            candidate_result=SimpleNamespace(report_count=1, chunk_count=1),
            publication_outcome=SimpleNamespace(publication_generation=2, write_epoch=1),
            attempted_report_uids=("report-1",),
            failed_report_uids=(),
        )

    monkeypatch.setattr(continuous_update, "execute_continuous_update", fake_execute)

    assert embed_pipeline.run_pipeline() == 0
    assert captured["extractor_name"] == "pymupdf"
    assert captured["allow_extraction_fallback"] is True
    assert captured["fallback_extractor_name"] == "opendataloader"
    assert captured["use_parent_child"] is True
    assert (
        captured["batch_size"]
        == continuous_update.DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE
    )
