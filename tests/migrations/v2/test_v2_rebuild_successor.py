from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrations.v2 import rebuild_v2_successor


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_known_reversed_default_policy_is_repaired_without_touching_other_env_values(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENROUTER_API_KEY=keep-this-value",
                "EXTRACTION_ENGINE=pymupdf",
                "PDF_EXTRACTION_ENGINE=opendataloader",
                "PDF_EXTRACTION_FALLBACK_ENGINE=pymupdf",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert rebuild_v2_successor.repair_known_reversed_default_policy(env_path)

    values = _env_values(env_path)
    assert values["OPENROUTER_API_KEY"] == "keep-this-value"
    assert values["EXTRACTION_ENGINE"] == "pymupdf"
    assert values["PDF_EXTRACTION_ENGINE"] == "pymupdf"
    assert values["PDF_EXTRACTION_FALLBACK_ENGINE"] == "opendataloader"
    assert values["UNEMBEDDED_PDF_EXTRACTION_ENGINE"] == "pymupdf"


def test_historical_opendataloader_pending_default_is_repaired(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENROUTER_API_KEY=keep-this-value",
                "PDF_EXTRACTION_ENGINE=pymupdf",
                "UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert rebuild_v2_successor.repair_known_reversed_default_policy(env_path)

    values = _env_values(env_path)
    assert values["OPENROUTER_API_KEY"] == "keep-this-value"
    assert values["PDF_EXTRACTION_ENGINE"] == "pymupdf"
    assert values["PDF_EXTRACTION_FALLBACK_ENGINE"] == "opendataloader"
    assert values["UNEMBEDDED_PDF_EXTRACTION_ENGINE"] == "pymupdf"


def test_repair_removes_duplicate_extraction_keys_so_last_value_cannot_win(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "EXTRACTION_ENGINE=pymupdf",
                "PDF_EXTRACTION_ENGINE=pymupdf",
                "PDF_EXTRACTION_ENGINE=opendataloader",
                "PDF_EXTRACTION_FALLBACK_ENGINE=pymupdf",
                "UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert rebuild_v2_successor.repair_known_reversed_default_policy(env_path)

    content = env_path.read_text(encoding="utf-8")
    values = _env_values(env_path)
    keys = [
        line.split("=", 1)[0]
        for line in content.splitlines()
        if "=" in line
    ]
    assert keys.count("PDF_EXTRACTION_ENGINE") == 1
    assert keys.count("PDF_EXTRACTION_FALLBACK_ENGINE") == 1
    assert keys.count("UNEMBEDDED_PDF_EXTRACTION_ENGINE") == 1
    assert values["PDF_EXTRACTION_ENGINE"] == "pymupdf"
    assert values["PDF_EXTRACTION_FALLBACK_ENGINE"] == "opendataloader"
    assert values["UNEMBEDDED_PDF_EXTRACTION_ENGINE"] == "pymupdf"


def test_custom_extraction_policy_is_not_overwritten(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    original = (
        "EXTRACTION_ENGINE=pymupdf\n"
        "PDF_EXTRACTION_ENGINE=marker\n"
        "PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader\n"
    )
    env_path.write_text(original, encoding="utf-8")

    assert not rebuild_v2_successor.repair_known_reversed_default_policy(env_path)
    assert env_path.read_text(encoding="utf-8") == original


def test_configured_policy_matches_incremental_embedding_policy() -> None:
    config = SimpleNamespace(
        EXTRACTION_ENGINE="pymupdf",
        EXTRACTION_FALLBACK_ENGINE="opendataloader",
        UNEMBEDDED_EXTRACTION_ENGINE="pymupdf",
    )

    policy = rebuild_v2_successor.configured_extraction_policy(config)

    assert policy.primary == "pymupdf"
    assert policy.fallback == "opendataloader"
    assert policy.allow_fallback
    assert policy.profile == "pymupdf|fallback=opendataloader"


def test_pending_extractor_override_disables_global_fallback() -> None:
    config = SimpleNamespace(
        EXTRACTION_ENGINE="pymupdf",
        EXTRACTION_FALLBACK_ENGINE="opendataloader",
        UNEMBEDDED_EXTRACTION_ENGINE="marker",
    )

    policy = rebuild_v2_successor.configured_extraction_policy(config)

    assert policy.primary == "marker"
    assert policy.fallback is None
    assert not policy.allow_fallback
    assert policy.profile == "marker"


def test_progress_extractor_reports_each_pdf_and_preserves_fallback_policy(
    monkeypatch,
    capsys,
) -> None:
    from src.core import pdf_extraction

    policy = rebuild_v2_successor.ExtractionPolicy(
        primary="pymupdf",
        fallback="opendataloader",
        allow_fallback=True,
        profile="pymupdf|fallback=opendataloader",
    )
    captured: dict[str, object] = {}
    expected = object()

    def fake_extract(path, engine, **kwargs):
        captured["path"] = path
        captured["engine"] = engine
        captured["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(pdf_extraction, "extract_pdf_text", fake_extract)
    extractor = rebuild_v2_successor.progress_extractor(policy, total=2)

    result = extractor(Path("first.pdf"), "pymupdf")

    assert result is expected
    assert captured["path"] == "first.pdf"
    assert captured["engine"] == "pymupdf"
    assert captured["kwargs"] == {
        "clean": True,
        "allow_fallback": True,
        "fallback_engine": "opendataloader",
    }
    assert "[PDF 1/2] first.pdf" in capsys.readouterr().out


def test_progress_extractor_ignores_a_closed_output_stream(monkeypatch) -> None:
    from src.core import pdf_extraction

    policy = rebuild_v2_successor.ExtractionPolicy(
        primary="pymupdf",
        fallback="opendataloader",
        allow_fallback=True,
        profile="pymupdf|fallback=opendataloader",
    )
    extracted_paths: list[str] = []

    def fake_extract(path, _engine, **_kwargs):
        extracted_paths.append(path)
        return f"text from {path}"

    def closed_output(*_args, **_kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(pdf_extraction, "extract_pdf_text", fake_extract)
    monkeypatch.setattr("builtins.print", closed_output)
    extractor = rebuild_v2_successor.progress_extractor(policy, total=2)

    assert extractor(Path("first.pdf"), "pymupdf") == "text from first.pdf"
    assert extractor(Path("second.pdf"), "pymupdf") == "text from second.pdf"
    assert extracted_paths == ["first.pdf", "second.pdf"]


def test_progress_extractor_rejects_an_unsupported_unexercised_fallback() -> None:
    policy = rebuild_v2_successor.ExtractionPolicy(
        primary="pymupdf",
        fallback="not-a-real-engine",
        allow_fallback=True,
        profile="pymupdf|fallback=not-a-real-engine",
    )

    with pytest.raises(ValueError, match="Unsupported extraction engine"):
        rebuild_v2_successor.progress_extractor(policy, total=1)


def test_inspection_uses_read_only_runtime_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.retrieval import bootstrap

    source_root = tmp_path / "downloaded"
    source_root.mkdir()
    (source_root / "report.pdf").write_bytes(b"%PDF-1.4\n")
    selection = SimpleNamespace(
        is_native=True,
        active_snapshot_id="active-old",
        active_build_id="build-old",
    )
    config = SimpleNamespace(
        DB_PATH=str(tmp_path / "reports.db"),
        SAVE_DIR=str(source_root),
        EXTRACTION_ENGINE="pymupdf",
        EXTRACTION_FALLBACK_ENGINE="opendataloader",
        UNEMBEDDED_EXTRACTION_ENGINE="pymupdf",
    )
    monkeypatch.setattr(
        bootstrap,
        "inspect_runtime",
        lambda _db_path: selection,
    )
    monkeypatch.setattr(
        bootstrap,
        "reconcile_and_inspect_runtime",
        lambda _db_path: (_ for _ in ()).throw(
            AssertionError("read-only inspection attempted reconciliation")
        ),
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "_active_profile",
        lambda _selection: ("opendataloader|fallback=pymupdf", 1),
    )

    inspection = rebuild_v2_successor.inspect_rebuild(config)

    assert inspection.active_snapshot_id == "active-old"
    assert inspection.requested_profile == "pymupdf|fallback=opendataloader"
    assert inspection.source_pdf_count == 1


def test_execute_rebuild_publishes_with_the_configured_incremental_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.core import embed_pipeline
    from src.retrieval import build_service

    source_root = tmp_path / "downloaded"
    source_root.mkdir()
    config = SimpleNamespace(
        DB_PATH=str(tmp_path / "reports.db"),
        SAVE_DIR=str(source_root),
        EMBEDDING_MODEL="model-a",
        EXTRACTION_ENGINE="pymupdf",
        EXTRACTION_FALLBACK_ENGINE="opendataloader",
        UNEMBEDDED_EXTRACTION_ENGINE="pymupdf",
        USE_PARENT_CHILD=True,
        CHUNK_SIZE=500,
        PARENT_CHUNK_SIZE=1000,
        CHILD_CHUNK_SIZE=250,
    )
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-old",
        active_profile="opendataloader|fallback=pymupdf",
        requested_profile="pymupdf|fallback=opendataloader",
        active_report_count=1,
        source_pdf_count=2,
        profile_matches=False,
    )
    embeddings = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", lambda: embeddings)

    def fake_execute(db_path, source_directory, **kwargs):
        captured["db_path"] = db_path
        captured["source_directory"] = source_directory
        captured["kwargs"] = kwargs
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

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert captured["db_path"] == config.DB_PATH
    assert captured["source_directory"] == config.SAVE_DIR
    assert kwargs["embeddings"] is embeddings
    assert kwargs["extractor_name"] == "pymupdf"
    assert kwargs["fallback_extractor_name"] == "opendataloader"
    assert kwargs["allow_extraction_fallback"] is True
    assert callable(kwargs["extractor"])
    assert kwargs["use_parent_child"] is True
    assert result.previous_snapshot_id == "active-old"
    assert result.active_snapshot_id == "active-new"
    assert result.report_count == 2
    assert result.indexed_report_count == 1
    assert result.extraction_failure_count == 1


def test_execute_rebuild_uses_explicit_repaired_policy_and_candidate_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.core import embed_pipeline
    from src.retrieval import build_service

    source_root = tmp_path / "downloaded"
    source_root.mkdir()
    config = SimpleNamespace(
        DB_PATH=str(tmp_path / "reports.db"),
        SAVE_DIR=str(source_root),
        EMBEDDING_MODEL="model-a",
        EXTRACTION_ENGINE="opendataloader",
        EXTRACTION_FALLBACK_ENGINE="pymupdf",
        UNEMBEDDED_EXTRACTION_ENGINE="opendataloader",
        USE_PARENT_CHILD=True,
        CHUNK_SIZE=500,
        PARENT_CHUNK_SIZE=1000,
        CHILD_CHUNK_SIZE=250,
    )
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-old",
        active_profile="opendataloader|fallback=pymupdf",
        requested_profile="pymupdf|fallback=opendataloader",
        active_report_count=1,
        source_pdf_count=1,
        profile_matches=False,
    )
    policy = rebuild_v2_successor.default_extraction_policy()
    captured: dict[str, object] = {}
    monkeypatch.setattr(embed_pipeline, "build_embeddings_fn", object)

    def fake_execute(_db_path, _source_directory, **kwargs):
        captured.update(kwargs)
        return (
            SimpleNamespace(
                report_count=2,
                indexed_report_count=2,
                extraction_failure_count=0,
            ),
            SimpleNamespace(
                active_snapshot_id="active-new",
                publication_generation=3,
                write_epoch=2,
            ),
        )

    monkeypatch.setattr(build_service, "execute_full_corpus_successor", fake_execute)

    result = rebuild_v2_successor.execute_rebuild(
        config,
        inspection,
        policy=policy,
    )

    assert captured["extractor_name"] == "pymupdf"
    assert captured["fallback_extractor_name"] == "opendataloader"
    assert result.report_count == 2


def test_check_mode_is_read_only_and_reports_profile_mismatch(
    monkeypatch,
    capsys,
) -> None:
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-old",
        active_profile="opendataloader|fallback=pymupdf",
        requested_profile="pymupdf|fallback=opendataloader",
        active_report_count=140,
        source_pdf_count=347,
        profile_matches=False,
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "load_config",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "inspect_rebuild",
        lambda _config: inspection,
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "execute_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("check mode performed a rebuild")
        ),
    )

    assert (
        rebuild_v2_successor.main(
            ["--check", "--use-configured-policy"],
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "opendataloader|fallback=pymupdf" in output
    assert "pymupdf|fallback=opendataloader" in output
    assert "tools\\recovery\\REBUILD_V2.bat" in output


def test_check_previews_default_policy_for_historical_opendataloader_config(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    env_path = tmp_path / ".env"
    original = (
        "PDF_EXTRACTION_ENGINE=pymupdf\n"
        "UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader\n"
    )
    env_path.write_text(original, encoding="utf-8")
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-old",
        active_profile="opendataloader",
        requested_profile="opendataloader",
        active_report_count=140,
        source_pdf_count=347,
        profile_matches=True,
    )
    monkeypatch.setattr(rebuild_v2_successor, "ENV_PATH", env_path)
    monkeypatch.setattr(
        rebuild_v2_successor,
        "load_config",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "inspect_rebuild",
        lambda _config: inspection,
    )

    assert rebuild_v2_successor.main(["--check"]) == 0

    output = capsys.readouterr().out
    assert "현재 추출 프로필: opendataloader" in output
    assert "재생성 추출 프로필: pymupdf|fallback=opendataloader" in output
    assert "[조치 필요]" in output
    assert env_path.read_text(encoding="utf-8") == original


def test_cancelled_rebuild_does_not_repair_env(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    env_path = tmp_path / ".env"
    original = (
        "PDF_EXTRACTION_ENGINE=pymupdf\n"
        "UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader\n"
    )
    env_path.write_text(original, encoding="utf-8")
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-old",
        active_profile="opendataloader",
        requested_profile="opendataloader",
        active_report_count=140,
        source_pdf_count=347,
        profile_matches=True,
    )
    monkeypatch.setattr(rebuild_v2_successor, "ENV_PATH", env_path)
    monkeypatch.setattr(rebuild_v2_successor, "load_config", SimpleNamespace)
    monkeypatch.setattr(
        rebuild_v2_successor,
        "inspect_rebuild",
        lambda _config: inspection,
    )

    assert rebuild_v2_successor.main([]) == 3

    assert "[중단]" in capsys.readouterr().out
    assert env_path.read_text(encoding="utf-8") == original


def test_failed_preflight_does_not_repair_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_path = tmp_path / ".env"
    original = (
        "PDF_EXTRACTION_ENGINE=pymupdf\n"
        "UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader\n"
    )
    env_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(rebuild_v2_successor, "ENV_PATH", env_path)
    monkeypatch.setattr(rebuild_v2_successor, "load_config", SimpleNamespace)
    monkeypatch.setattr(
        rebuild_v2_successor,
        "inspect_rebuild",
        lambda _config: (_ for _ in ()).throw(RuntimeError("no active V2")),
    )

    assert rebuild_v2_successor.main(["--yes"]) == 2
    assert env_path.read_text(encoding="utf-8") == original


def test_matching_active_profile_repairs_historical_env_without_rebuilding(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PDF_EXTRACTION_ENGINE=pymupdf\n"
        "UNEMBEDDED_PDF_EXTRACTION_ENGINE=opendataloader\n",
        encoding="utf-8",
    )
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-new",
        active_profile="pymupdf|fallback=opendataloader",
        requested_profile="opendataloader",
        active_report_count=347,
        source_pdf_count=347,
        profile_matches=False,
    )
    monkeypatch.setattr(rebuild_v2_successor, "ENV_PATH", env_path)
    monkeypatch.setattr(rebuild_v2_successor, "load_config", SimpleNamespace)
    monkeypatch.setattr(
        rebuild_v2_successor,
        "inspect_rebuild",
        lambda _config: inspection,
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "execute_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("matching active profile was rebuilt")
        ),
    )

    assert rebuild_v2_successor.main(["--yes"]) == 0

    values = _env_values(env_path)
    assert values["PDF_EXTRACTION_ENGINE"] == "pymupdf"
    assert values["PDF_EXTRACTION_FALLBACK_ENGINE"] == "opendataloader"
    assert values["UNEMBEDDED_PDF_EXTRACTION_ENGINE"] == "pymupdf"
    assert "재생성이 필요하지 않습니다" in capsys.readouterr().out


def test_force_rebuilds_even_when_active_profile_matches(
    monkeypatch,
    capsys,
) -> None:
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-old",
        active_profile="pymupdf|fallback=opendataloader",
        requested_profile="pymupdf|fallback=opendataloader",
        active_report_count=381,
        source_pdf_count=4955,
        profile_matches=True,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        rebuild_v2_successor,
        "load_config",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "inspect_rebuild",
        lambda _config: inspection,
    )

    def fake_execute(_config, actual_inspection, *, policy):
        captured["inspection"] = actual_inspection
        captured["policy"] = policy
        return SimpleNamespace(
            previous_snapshot_id="active-old",
            active_snapshot_id="active-new",
            active_profile="pymupdf|fallback=opendataloader",
            report_count=4955,
            indexed_report_count=4955,
            extraction_failure_count=0,
        )

    monkeypatch.setattr(
        rebuild_v2_successor,
        "execute_rebuild",
        fake_execute,
    )

    assert rebuild_v2_successor.main(["--yes", "--force"]) == 0

    assert captured == {"inspection": inspection, "policy": None}
    output = capsys.readouterr().out
    assert "현재 snapshot: active-new" in output
    assert "원본 PDF: 4955개" in output


def test_rebuild_failure_reports_that_the_active_snapshot_was_preserved(
    monkeypatch,
    capsys,
) -> None:
    inspection = rebuild_v2_successor.RebuildInspection(
        active_snapshot_id="active-old",
        active_profile="opendataloader|fallback=pymupdf",
        requested_profile="pymupdf|fallback=opendataloader",
        active_report_count=140,
        source_pdf_count=347,
        profile_matches=False,
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "load_config",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "inspect_rebuild",
        lambda _config: inspection,
    )
    monkeypatch.setattr(
        rebuild_v2_successor,
        "execute_rebuild",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
    )

    assert (
        rebuild_v2_successor.main(
            ["--yes", "--use-configured-policy"],
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "provider unavailable" in output
    assert "active-old" in output
    assert "기존 V2 snapshot은 그대로 유지됩니다" in output


def test_windows_entrypoint_uses_successor_script_without_deleting_v2_in_place() -> None:
    source = (
        REPOSITORY_ROOT / "tools" / "recovery" / "REBUILD_V2.bat"
    ).read_text(encoding="utf-8-sig")

    assert "scripts\\migrations\\v2\\rebuild_v2_successor.py" in source
    assert 'if /I "%~1"=="--check" goto run_direct' in source
    assert '"%VENV_PYTHON%" "%REBUILD_SCRIPT%" --yes %*' in source
    assert "data\\reports.db" in source
    assert "data\\downloaded" in source
    assert "rmdir " not in source.lower()
    assert "del " not in source.lower()
