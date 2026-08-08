from __future__ import annotations

import json

from src.retrieval.support_export import build_support_payload, write_support_export
from tests.retrieval.native_build_fixtures import _native_seed


def test_support_export_is_deterministic_and_contains_only_redacted_native_evidence(
    tmp_path,
):
    data_root, _sources = _native_seed(tmp_path)

    first = build_support_payload(data_root)
    second = build_support_payload(data_root)

    assert first == second
    assert first["runtime"]["write_epoch"] == 1
    assert (
        first["active_revision"]["membership_count"]
        == first["active_revision"]["ntotal"]
    )
    assert set(first["runtime"]) == {
        "mode",
        "schema_version",
        "active_snapshot_id",
        "active_build_id",
        "predecessor_snapshot_id",
        "publication_generation",
        "write_epoch",
        "degraded",
        "write_enabled",
    }
    output = write_support_export(tmp_path / "support.json", first)
    payload = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload == first
    assert str(tmp_path) not in serialized
    assert "abcdef" not in serialized
    assert "company.pdf" not in serialized
    assert "query" not in serialized.lower()
