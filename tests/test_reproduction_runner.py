from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from apps.cli import reproduction_runner
from src.core import release_assets


def _snapshot(
    root: Path,
    *,
    generation: int = 1,
    write_epoch: int = 0,
    active_build_id: str = "projected-build-1",
) -> Path:
    root.mkdir(parents=True)
    catalog = root / "projected_catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    connection.execute(
        """
        CREATE TABLE retrieval_runtime (
            runtime_id INTEGER PRIMARY KEY,
            publication_generation INTEGER NOT NULL,
            write_epoch INTEGER NOT NULL,
            active_snapshot_id TEXT,
            active_build_id TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO retrieval_runtime
        VALUES (1, ?, ?, 'projected-snapshot-1', ?)
        """,
        (generation, write_epoch, active_build_id),
    )
    connection.commit()
    connection.close()
    (root / "subset.faiss").write_bytes(b"index")
    catalog_bytes = catalog.read_bytes()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "fixed_snapshot_revision_id": "snapshot-1",
                "projected_snapshot_id": "projected-snapshot-1",
                "projected_build_id": "projected-build-1",
                "files": {
                    "projected_catalog.sqlite3": {
                        "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
                        "size_bytes": len(catalog_bytes),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def test_prepare_isolated_data_root_uses_only_snapshot_bytes(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot")
    checkout_data = tmp_path / "current-data"
    checkout_data.mkdir()
    (checkout_data / "catalog.sqlite3").write_bytes(b"must-not-be-used")

    data_root = reproduction_runner.prepare_isolated_data_root(
        snapshot, tmp_path / "workspace"
    )

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    checkpoint = (
        data_root
        / "retrieval"
        / "v2"
        / "backups"
        / "catalog-current-g1-fixed-snapshot-projection.sqlite3"
    )
    floor_path = (
        data_root
        / "retrieval"
        / "v2"
        / "evidence"
        / "fixed-snapshot-projection"
        / "committed-floor.json"
    )
    assert catalog.read_bytes() == (snapshot / "projected_catalog.sqlite3").read_bytes()
    assert (data_root / "subset.faiss").read_bytes() == b"index"
    assert (data_root / "fixed-snapshot-manifest.json").is_file()
    assert checkpoint.read_bytes() == catalog.read_bytes()
    assert floor_path.read_bytes().startswith(b'{"active_snapshot_id"')
    assert not floor_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert json.loads(floor_path.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "publication_id": "fixed-snapshot-projection",
        "publication_generation": 1,
        "write_epoch": 0,
        "active_snapshot_id": "projected-snapshot-1",
        "checkpoint_relative_path": (
            "retrieval/v2/backups/"
            "catalog-current-g1-fixed-snapshot-projection.sqlite3"
        ),
        "checkpoint_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
    }

    assert reproduction_runner.prepare_isolated_data_root(
        snapshot, tmp_path / "workspace"
    ) == data_root


def test_prepare_isolated_data_root_generation_zero_needs_no_floor(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot", generation=0)

    data_root = reproduction_runner.prepare_isolated_data_root(
        snapshot, tmp_path / "workspace"
    )

    assert not (data_root / "retrieval" / "v2" / "evidence").exists()


def test_prepare_isolated_data_root_rejects_catalog_identity_mismatch(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot")
    with (snapshot / "projected_catalog.sqlite3").open("ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(RuntimeError, match="bytes do not match manifest"):
        reproduction_runner.prepare_isolated_data_root(
            snapshot, tmp_path / "workspace"
        )


def test_prepare_isolated_data_root_rejects_conflicting_preseeded_floor(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot")
    floor = (
        tmp_path
        / "workspace"
        / "isolated-data"
        / "retrieval"
        / "v2"
        / "evidence"
        / "fixed-snapshot-projection"
        / "committed-floor.json"
    )
    floor.parent.mkdir(parents=True)
    floor.write_bytes(b"conflicting evidence\n")

    with pytest.raises(RuntimeError, match="runtime evidence conflicts"):
        reproduction_runner.prepare_isolated_data_root(
            snapshot, tmp_path / "workspace"
        )


def test_prepare_isolated_data_root_rejects_projected_build_mismatch(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot", active_build_id="unexpected-build"
    )

    with pytest.raises(RuntimeError, match="runtime identity is invalid"):
        reproduction_runner.prepare_isolated_data_root(
            snapshot, tmp_path / "workspace"
        )


@pytest.mark.parametrize(
    ("generation", "write_epoch"),
    [(2, 0), (1, 1)],
)
def test_prepare_isolated_data_root_rejects_unproven_positive_runtime_floor(
    tmp_path: Path,
    generation: int,
    write_epoch: int,
) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot",
        generation=generation,
        write_epoch=write_epoch,
    )

    with pytest.raises(RuntimeError, match="runtime floor is unsupported"):
        reproduction_runner.prepare_isolated_data_root(
            snapshot, tmp_path / "workspace"
        )


def test_prepare_isolated_data_root_has_no_missing_file_fallback(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "snapshot")
    (snapshot / "subset.faiss").unlink()

    with pytest.raises(RuntimeError, match="incomplete: subset.faiss"):
        reproduction_runner.prepare_isolated_data_root(
            snapshot, tmp_path / "workspace"
        )


def test_run_artifact_projects_only_safe_evidence() -> None:
    artifact = reproduction_runner.build_run_artifact(
        {
            "generation": "영업이익은 10입니다.",
            "route": "vectordb",
            "rerank_info": [
                {
                    "source_uid": "report-1",
                    "source_sha256": "a" * 64,
                    "chunk_uid": "chunk-1",
                    "rank": 1,
                },
                {
                    "source_uid": "unsafe",
                    "source_sha256": "not-a-digest",
                    "local_path": "C:/private/source.pdf",
                },
            ],
            "citation_ranks_used": [1],
        },
        latency_ms=12.3456,
        runtime_profile={"model": "fixture-model"},
    )

    assert artifact["raw_answer"] == "영업이익은 10입니다."
    assert artifact["evidence_refs"] == [
        {
            "role": "CITED",
            "source_uid": "report-1",
            "source_sha256": "a" * 64,
            "chunk_uid": "chunk-1",
            "rank": 1,
        }
    ]
    assert "local_path" not in json.dumps(artifact)


def test_runtime_profile_controls_non_secret_execution_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GENERATION_MODEL", raising=False)
    reproduction_runner.apply_runtime_profile_environment(
        {
            "environment": {
                "GENERATION_MODEL": "pinned/model-v1",
                "USE_RERANKER": True,
                "SEARCH_TOP_K": 7,
            }
        }
    )

    assert reproduction_runner.os.environ["GENERATION_MODEL"] == "pinned/model-v1"
    assert reproduction_runner.os.environ["USE_RERANKER"] == "true"
    assert reproduction_runner.os.environ["SEARCH_TOP_K"] == "7"
    with pytest.raises(RuntimeError, match="not allowed"):
        reproduction_runner.apply_runtime_profile_environment(
            {"environment": {"OPENROUTER_API_KEY": "must-not-be-persisted"}}
        )


def test_runtime_profile_keys_match_release_validation_contract() -> None:
    assert (
        reproduction_runner._PROFILE_ENVIRONMENT_KEYS
        == release_assets.RUNTIME_PROFILE_ENVIRONMENT_KEYS
    )
