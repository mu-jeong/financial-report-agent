from __future__ import annotations

import importlib
from pathlib import Path
import sqlite3

import pytest

from apps.gui import monitoring_views, sidebar_views, status_cache


@pytest.fixture(autouse=True)
def _clear_status_cache() -> None:
    status_cache.clear()
    yield
    status_cache.clear()


def _status_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data_root = tmp_path / "data"
    db_path = data_root / "reports.db"
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    save_dir = data_root / "downloaded"
    faiss_dir = data_root / "vector_db"

    catalog.parent.mkdir(parents=True)
    save_dir.mkdir(parents=True)
    faiss_dir.mkdir(parents=True)
    db_path.write_bytes(b"legacy-anchor")
    with sqlite3.connect(catalog) as connection:
        connection.executescript(
            """
            CREATE TABLE retrieval_runtime (
                runtime_id INTEGER PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                active_snapshot_id TEXT,
                active_build_id TEXT,
                predecessor_snapshot_id TEXT,
                publication_generation INTEGER NOT NULL,
                write_epoch INTEGER NOT NULL,
                v1_fallback_open INTEGER NOT NULL,
                degraded INTEGER NOT NULL,
                write_enabled INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO retrieval_runtime VALUES (
                1, 7, 'snapshot-1', 'build-1', NULL,
                1, 1, 0, 0, 1, '2026-08-04T00:00:00Z'
            );
            CREATE TABLE retrieval_delta_segments (
                segment_id TEXT PRIMARY KEY,
                base_snapshot_id TEXT NOT NULL,
                base_publication_generation INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                relative_path TEXT,
                state TEXT NOT NULL,
                state_changed_at TEXT NOT NULL
            );
            """
        )
    return save_dir, db_path, faiss_dir, catalog


def _load_status(
    save_dir: Path,
    db_path: Path,
    faiss_dir: Path,
) -> dict:
    return status_cache.get_data_status(
        save_dir=str(save_dir),
        db_path=str(db_path),
        faiss_dir=str(faiss_dir),
    )


def test_unchanged_revision_computes_status_once_across_sidebar_reload(
    tmp_path,
    monkeypatch,
):
    save_dir, db_path, faiss_dir, _catalog = _status_paths(tmp_path)
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {"db": {"total_reports": 7}}

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(status_cache.data_update_jobs, "read_status", lambda: None)

    first = _load_status(save_dir, db_path, faiss_dir)
    importlib.reload(sidebar_views)
    second = _load_status(save_dir, db_path, faiss_dir)

    assert first == second == {"db": {"total_reports": 7}}
    assert calls == ["load"]


def test_catalog_replacement_invalidates_cached_status(tmp_path, monkeypatch):
    save_dir, db_path, faiss_dir, catalog = _status_paths(tmp_path)
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {"load_count": len(calls)}

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(status_cache.data_update_jobs, "read_status", lambda: None)

    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 1
    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 1
    catalog.write_bytes(b"catalog-was-replaced")
    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 2


def test_committed_catalog_wal_change_invalidates_cached_status(tmp_path, monkeypatch):
    save_dir, db_path, faiss_dir, catalog = _status_paths(tmp_path)
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {"load_count": len(calls)}

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(status_cache.data_update_jobs, "read_status", lambda: None)

    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 1
    writer = sqlite3.connect(catalog)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute(
            """
            UPDATE retrieval_runtime
            SET write_epoch = 2, updated_at = '2026-08-04T00:00:01Z'
            WHERE runtime_id = 1
            """
        )
        writer.commit()

        assert Path(f"{catalog}-wal").is_file()
        assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 2
    finally:
        writer.close()


def test_ready_delta_invalidates_without_runtime_generation_change(
    tmp_path,
    monkeypatch,
):
    save_dir, db_path, faiss_dir, catalog = _status_paths(tmp_path)
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {"load_count": len(calls)}

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(status_cache.data_update_jobs, "read_status", lambda: None)

    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 1
    with sqlite3.connect(catalog) as writer:
        writer.execute(
            """
            INSERT INTO retrieval_delta_segments VALUES (
                'segment-1', 'snapshot-1', 1, 1, NULL,
                'ready', '2026-08-04T00:00:01Z'
            )
            """
        )
    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 2


def test_update_job_state_change_invalidates_cached_status(tmp_path, monkeypatch):
    save_dir, db_path, faiss_dir, _catalog = _status_paths(tmp_path)
    job_status = {"state": "running", "phase": "download", "pid": 101}
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {"load_count": len(calls)}

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(
        status_cache.data_update_jobs,
        "read_status",
        lambda: dict(job_status),
    )

    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 1
    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 1
    job_status.update({"state": "succeeded", "phase": "done"})
    assert _load_status(save_dir, db_path, faiss_dir)["load_count"] == 2


def test_cached_status_returns_mutation_isolated_snapshots(tmp_path, monkeypatch):
    save_dir, db_path, faiss_dir, _catalog = _status_paths(tmp_path)
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {
            "db": {
                "total_reports": 1,
                "report_date_counts": {"2026-08-04": 1},
            }
        }

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(status_cache.data_update_jobs, "read_status", lambda: None)

    first = _load_status(save_dir, db_path, faiss_dir)
    first["db"]["total_reports"] = 999
    first["db"]["report_date_counts"]["2026-08-04"] = 999
    second = _load_status(save_dir, db_path, faiss_dir)

    assert second["db"] == {
        "total_reports": 1,
        "report_date_counts": {"2026-08-04": 1},
    }
    assert calls == ["load"]


def test_pdf_count_stays_fresh_without_recomputing_catalog_status(
    tmp_path,
    monkeypatch,
):
    save_dir, db_path, faiss_dir, _catalog = _status_paths(tmp_path)
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {"downloaded_pdfs": 0}

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(status_cache.data_update_jobs, "read_status", lambda: None)

    assert _load_status(save_dir, db_path, faiss_dir)["downloaded_pdfs"] == 0
    (save_dir / "new-report.PDF").write_bytes(b"pdf")
    assert _load_status(save_dir, db_path, faiss_dir)["downloaded_pdfs"] == 1
    assert calls == ["load"]


def test_native_monitoring_reuses_sidebar_snapshot(tmp_path, monkeypatch):
    save_dir, db_path, faiss_dir, _catalog = _status_paths(tmp_path)
    calls = []

    def fake_get_data_status(**_kwargs):
        calls.append("load")
        return {"retrieval": {"mode": "native"}}

    def unexpected_native_status(**_kwargs):
        raise AssertionError("native status should reuse the healthy sidebar snapshot")

    monkeypatch.setattr(status_cache.status_module, "get_data_status", fake_get_data_status)
    monkeypatch.setattr(
        status_cache.status_module,
        "get_native_v2_data_status",
        unexpected_native_status,
    )
    monkeypatch.setattr(status_cache.data_update_jobs, "read_status", lambda: None)

    _load_status(save_dir, db_path, faiss_dir)
    monitoring = status_cache.get_native_v2_data_status(
        save_dir=str(save_dir),
        db_path=str(db_path),
    )

    assert monitoring["retrieval"]["mode"] == "native"
    assert calls == ["load"]


def test_global_monitoring_rechecks_legacy_status_but_reuses_native_status(
    monkeypatch,
):
    native_only_status = {"retrieval": {"mode": "unavailable"}}
    calls = []

    def fake_native_status():
        calls.append("native-only")
        return native_only_status

    monkeypatch.setattr(
        monitoring_views.status_cache,
        "get_native_v2_data_status",
        fake_native_status,
    )
    legacy_status = {"retrieval": {"mode": "legacy_v1"}}
    native_status = {"retrieval": {"mode": "native"}}

    assert (
        monitoring_views._resolve_global_monitoring_status(legacy_status)
        is native_only_status
    )
    assert (
        monitoring_views._resolve_global_monitoring_status(native_status)
        is native_status
    )
    assert calls == ["native-only"]


def test_evidence_revision_tracks_changes_without_resolving_every_file(
    tmp_path,
    monkeypatch,
):
    evidence_dir = tmp_path / "retrieval" / "v2" / "evidence" / "publication-1"
    evidence_dir.mkdir(parents=True)
    floor = evidence_dir / "committed-floor.json"
    floor.write_text('{"generation": 1}', encoding="utf-8")
    original_resolve = Path.resolve

    def guarded_resolve(path, *args, **kwargs):
        if evidence_dir in path.parents or path == evidence_dir:
            raise AssertionError("evidence children must not be resolved individually")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)

    first = status_cache._evidence_revision(tmp_path)
    floor.write_text('{"generation": 2, "changed": true}', encoding="utf-8")
    second = status_cache._evidence_revision(tmp_path)

    assert first != second


def test_sidebar_uses_process_persistent_status_cache_boundary():
    sidebar_source = Path("apps/gui/sidebar_views.py").read_text(encoding="utf-8-sig")
    app_source = Path("apps/gui/app.py").read_text(encoding="utf-8-sig")

    assert "status_cache.get_data_status()" in sidebar_source
    assert "status_module.get_data_status()" not in sidebar_source
    assert '"apps.gui.status_cache"' not in app_source
    assert "sidebar_status = sidebar_views.render_sidebar(current_id)" in app_source
    assert (
        "monitoring_views.render_global_monitoring_page(sidebar_status)"
        in app_source
    )
