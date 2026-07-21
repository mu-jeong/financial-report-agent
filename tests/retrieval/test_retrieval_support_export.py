from __future__ import annotations

import json

from src.retrieval.publication import PublicationCoordinator
from src.retrieval.support_export import build_support_payload, write_support_export
from tests.retrieval.test_retrieval_publication import make_native_install


def test_support_export_is_deterministic_and_contains_only_redacted_native_evidence(
    tmp_path,
):
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)

    first = build_support_payload(
        data_root / "reports.db",
        data_root=data_root,
    )
    second = build_support_payload(
        data_root / "reports.db",
        data_root=data_root,
    )

    assert first == second
    assert first["runtime"]["write_epoch"] == 1
    assert first["active_revision"]["membership_count"] == 1
    assert first["compatibility"]["state"] == "cleanup_pending"
    output = write_support_export(tmp_path / "support.json", first)
    payload = json.loads(output.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload == first
    assert str(tmp_path) not in serialized
    assert "abcdef" not in serialized
    assert "company.pdf" not in serialized
    assert "query" not in serialized.lower()
