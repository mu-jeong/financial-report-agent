from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrations.v2 import rebuild_v2_successor


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _config(tmp_path: Path) -> SimpleNamespace:
    source_root = tmp_path / "downloaded"
    source_root.mkdir()
    return SimpleNamespace(
        DATA_ROOT=str(tmp_path),
        SAVE_DIR=str(source_root),
        EMBEDDING_MODEL="model-a",
        PDF_EXTRACTION_ENGINE="pymupdf",
        PDF_EXTRACTION_FALLBACK_ENGINE="opendataloader",
        UNEMBEDDED_PDF_EXTRACTION_ENGINE="pymupdf",
        USE_PARENT_CHILD=True,
        CHUNK_SIZE=500,
        PARENT_CHUNK_SIZE=1000,
        CHILD_CHUNK_SIZE=250,
    )


def test_configured_policy_uses_native_pdf_settings(tmp_path: Path) -> None:
    policy = rebuild_v2_successor.configured_extraction_policy(_config(tmp_path))

    assert policy.primary == "pymupdf"
    assert policy.fallback == "opendataloader"
    assert policy.allow_fallback
    assert policy.profile == "pymupdf|fallback=opendataloader"


def test_pending_extractor_override_disables_global_fallback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.UNEMBEDDED_PDF_EXTRACTION_ENGINE = "marker"

    policy = rebuild_v2_successor.configured_extraction_policy(config)

    assert policy.primary == "marker"
    assert policy.fallback is None
    assert not policy.allow_fallback


def test_v1_import_profile_matches_configured_future_policy(tmp_path: Path) -> None:
    policy = rebuild_v2_successor.configured_extraction_policy(_config(tmp_path))

    assert rebuild_v2_successor.profile_matches_policy(
        "legacy-v1-import|configured=pymupdf|fallback=opendataloader",
        policy,
    )


def test_progress_extractor_preserves_fallback_policy(monkeypatch, capsys) -> None:
    from src.core import pdf_extraction

    captured: dict[str, object] = {}

    def fake_extract(path, engine, **kwargs):
        captured.update(path=path, engine=engine, kwargs=kwargs)
        return "text"

    monkeypatch.setattr(pdf_extraction, "extract_pdf_text", fake_extract)
    policy = rebuild_v2_successor.ExtractionPolicy(
        "pymupdf", "opendataloader", True, "pymupdf|fallback=opendataloader"
    )

    result = rebuild_v2_successor.progress_extractor(policy, total=1)(
        Path("report.pdf"), "pymupdf"
    )

    assert result == "text"
    assert captured == {
        "path": "report.pdf",
        "engine": "pymupdf",
        "kwargs": {
            "clean": True,
            "allow_fallback": True,
            "fallback_engine": "opendataloader",
        },
    }
    assert "[PDF 1/1] report.pdf" in capsys.readouterr().out


def test_progress_extractor_rejects_unsupported_fallback() -> None:
    policy = rebuild_v2_successor.ExtractionPolicy(
        "pymupdf", "not-an-engine", True, "invalid"
    )
    with pytest.raises(ValueError, match="Unsupported extraction engine"):
        rebuild_v2_successor.progress_extractor(policy, total=1)


def test_inspection_uses_data_root_and_read_only_runtime(tmp_path, monkeypatch) -> None:
    from src.retrieval import bootstrap

    config = _config(tmp_path)
    (Path(config.SAVE_DIR) / "report.pdf").write_bytes(b"%PDF-1.4\n")
    selection = SimpleNamespace(
        is_native=True,
        active_snapshot_id="active-old",
        active_build_id="build-old",
    )
    captured: list[str] = []
    monkeypatch.setattr(
        bootstrap,
        "inspect_runtime",
        lambda data_root: captured.append(data_root) or selection,
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "_active_profile",
        lambda _selection: ("opendataloader", 1),
    )

    inspection = rebuild_v2_successor.inspect_rebuild(config)

    assert captured == [config.DATA_ROOT]
    assert inspection.active_snapshot_id == "active-old"
    assert inspection.source_pdf_count == 1


def test_execute_rebuild_passes_data_root_to_native_successor(tmp_path, monkeypatch) -> None:
    from src.core import embed_pipeline
    from src.retrieval import build_service

    config = _config(tmp_path)
    inspection = rebuild_v2_successor.RebuildInspection(
        "active-old", "old-profile", "pymupdf|fallback=opendataloader", 1, 2, False
    )
    captured: dict[str, object] = {}
    embeddings = object()
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", lambda: embeddings)

    def fake_execute(data_root, source_directory, **kwargs):
        captured.update(data_root=data_root, source_directory=source_directory, kwargs=kwargs)
        return (
            SimpleNamespace(
                report_count=2,
                indexed_report_count=1,
                extraction_failure_count=1,
            ),
            SimpleNamespace(
                active_snapshot_id="active-new",
                publication_generation=3,
                write_epoch=2,
            ),
        )

    monkeypatch.setattr(build_service, "execute_full_corpus_successor", fake_execute)

    result = rebuild_v2_successor.execute_rebuild(config, inspection)

    assert captured["data_root"] == Path(config.DATA_ROOT)
    assert captured["source_directory"] == config.SAVE_DIR
    assert captured["kwargs"]["embeddings"] is embeddings
    assert result.active_snapshot_id == "active-new"


def test_check_mode_is_read_only(monkeypatch, capsys) -> None:
    inspection = rebuild_v2_successor.RebuildInspection(
        "active-old", "old", "new", 10, 20, False
    )
    monkeypatch.setattr(rebuild_v2_successor, "load_config", SimpleNamespace)
    monkeypatch.setattr(rebuild_v2_successor, "inspect_rebuild", lambda _config: inspection)
    monkeypatch.setattr(
        rebuild_v2_successor,
        "execute_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("check mode performed a rebuild")
        ),
    )

    assert rebuild_v2_successor.main(["--check"]) == 0
    assert "rebuild required" in capsys.readouterr().out


def test_matching_profile_skips_rebuild_without_force(monkeypatch) -> None:
    inspection = rebuild_v2_successor.RebuildInspection(
        "active", "profile", "profile", 10, 20, True
    )
    monkeypatch.setattr(rebuild_v2_successor, "load_config", SimpleNamespace)
    monkeypatch.setattr(rebuild_v2_successor, "inspect_rebuild", lambda _config: inspection)
    monkeypatch.setattr(
        rebuild_v2_successor,
        "execute_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching profile was rebuilt")
        ),
    )

    assert rebuild_v2_successor.main(["--yes"]) == 0


def test_rebuild_failure_reports_preserved_snapshot(monkeypatch, capsys) -> None:
    inspection = rebuild_v2_successor.RebuildInspection(
        "active-old", "old", "new", 10, 20, False
    )
    monkeypatch.setattr(rebuild_v2_successor, "load_config", SimpleNamespace)
    monkeypatch.setattr(rebuild_v2_successor, "inspect_rebuild", lambda _config: inspection)
    monkeypatch.setattr(
        rebuild_v2_successor,
        "execute_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )

    assert rebuild_v2_successor.main(["--yes"]) == 1
    output = capsys.readouterr().out
    assert "provider unavailable" in output
    assert "active-old" in output


def test_windows_entrypoint_is_native_v2_only() -> None:
    source = (REPOSITORY_ROOT / "tools" / "recovery" / "REBUILD_V2.bat").read_text(
        encoding="utf-8-sig"
    )

    assert "scripts\\migrations\\v2\\rebuild_v2_successor.py" in source
    assert 'if /I "%~1"=="--check" goto run_direct' in source
    assert '"%VENV_PYTHON%" "%REBUILD_SCRIPT%" --yes %*' in source
    assert "DATA_ROOT" in source
    assert "rmdir " not in source.lower()
    assert "del " not in source.lower()
