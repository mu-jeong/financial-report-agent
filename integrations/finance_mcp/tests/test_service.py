from __future__ import annotations

import json
import threading

import pytest

from integrations.finance_mcp.contracts import INPUT_MODELS, ToolFailure, ToolResult
from integrations.finance_mcp.service import ResearchService


class Adapter:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.revision = "revision-one"

    def list_reports(self, filters, limit, offset, expected_revision):
        self.calls.append((filters, limit, offset, expected_revision))
        if expected_revision and expected_revision != self.revision:
            raise ToolFailure("STALE_REVISION", "Search again.")
        return {"data": {"reports": [{"title": "한글 리포트"}]}, "revision": self.revision, "next_offset": offset + limit}

    def get_report_stats(self, filters, group_by, limit, offset, expected_revision):
        self.calls.append(group_by)
        return {"data": {"total_count": 7}, "revision": self.revision}

    def read_report(self, report_uid, expected_revision, offset, max_chars):
        self.calls.append((report_uid, expected_revision, offset, max_chars))
        return {"data": {"content": "본문"}, "revision": self.revision, "next_offset": offset + max_chars}

    def search_reports(self, query, filters, top_k):
        self.calls.append((query, filters, top_k))
        return {"data": {"results": []}, "revision": self.revision}

    def get_status(self):
        return {"data": {"state": "ready"}, "revision": self.revision}

    def close(self):
        self.closed = True


@pytest.fixture
def service():
    value = ResearchService(Adapter())
    yield value
    value.close()


@pytest.mark.parametrize("name,args", [
    ("get_status", {"extra": "secret"}),
    ("list_reports", {"limit": True}),
    ("list_reports", {"limit": "20"}),
    ("list_reports", {"limit": 101}),
    ("list_reports", {"filters": {"sql": "SELECT *"}}),
    ("list_reports", {"filters": {"target_names": [" "]}}),
    ("list_reports", {"filters": {"target_names": ["x"] * 101}}),
    ("list_reports", {"filters": {"report_types": ["unknown"]}}),
    ("list_reports", {"filters": {"report_date_start": "2026-02-30"}}),
    ("list_reports", {"filters": {"report_date_start": "2026-1-01"}}),
    ("list_reports", {"filters": {"report_date_start": "2026-09-10", "report_date_end": "2026-09-01"}}),
    ("get_report_stats", {"group_by": "password"}),
    ("search_reports", {"query": "  "}),
    ("search_reports", {"query": "x" * 4001}),
    ("search_reports", {"query": "x", "top_k": 31}),
    ("read_report", {"report_uid": "../.env"}),
    ("read_report", {"report_uid": "a" * 64, "max_chars": 30001}),
    ("read_report", {"report_uid": "a" * 64, "cursor": "x" * 4097}),
])
def test_invalid_input_never_calls_adapter(service, name, args):
    with pytest.raises(ToolFailure) as error:
        service.call(name, args)
    assert error.value.code == "INVALID_ARGUMENT"
    assert not service.adapter.calls
    assert "secret" not in error.value.message


def test_empty_array_and_absent_filter_remain_distinct(service):
    service.call("list_reports")
    service.call("list_reports", {"filters": {"target_names": []}})
    assert service.adapter.calls[0][0] == {}
    assert service.adapter.calls[1][0] == {"target_names": []}


def test_cursor_preserves_revision_and_offset_and_is_bounded(service):
    filters = {"target_names": ["한" * 200] * 100}
    first = service.call("list_reports", {"filters": filters, "limit": 2})
    token = first["meta"]["next_cursor"]
    assert len(token) < 1024
    second = service.call("list_reports", {"filters": filters, "cursor": token, "limit": 3})
    assert service.adapter.calls[-1] == (filters, 3, 2, "revision-one")
    assert second["meta"]["truncated"]
    ToolResult.model_validate(second)
    json.dumps(second, allow_nan=False)


def test_cursor_rejects_other_filter_tool_session_or_tampering(service):
    token = service.call("list_reports")["meta"]["next_cursor"]
    cases = [
        (service, "list_reports", {"cursor": token, "filters": {"brokers": ["다른 증권"]}}),
        (service, "get_report_stats", {"cursor": token}),
        (ResearchService(Adapter()), "list_reports", {"cursor": token}),
        (service, "list_reports", {"cursor": "!" + token[1:]}),
    ]
    for target, name, args in cases:
        with pytest.raises(ToolFailure) as error:
            target.call(name, args)
        assert error.value.code == "INVALID_ARGUMENT"


def test_revision_changed_between_pages(service):
    token = service.call("list_reports")["meta"]["next_cursor"]
    service.adapter.revision = "new-revision"
    with pytest.raises(ToolFailure, match="Search again") as error:
        service.call("list_reports", {"cursor": token})
    assert error.value.code == "STALE_REVISION"


def test_read_cursor_pins_revision_and_rejects_conflicting_revision(service):
    first = service.call("read_report", {"report_uid": "a" * 64, "max_chars": 10})
    token = first["meta"]["next_cursor"]
    service.call("read_report", {"report_uid": "a" * 64, "cursor": token})
    assert service.adapter.calls[-1][1:3] == ("revision-one", 10)
    with pytest.raises(ToolFailure) as error:
        service.call("read_report", {"report_uid": "a" * 64, "cursor": token, "expected_revision": "other"})
    assert error.value.code == "INVALID_ARGUMENT"


def test_busy_and_cleanup():
    started, finish = threading.Event(), threading.Event()

    class Blocking(Adapter):
        def get_status(self):
            started.set()
            assert finish.wait(5)
            return super().get_status()

    service = ResearchService(Blocking())
    results = []
    thread = threading.Thread(target=lambda: results.append(service.call("get_status")))
    thread.start()
    try:
        assert started.wait(5)
        with pytest.raises(ToolFailure) as error:
            service.call("get_status")
        assert error.value.code == "BUSY" and error.value.retryable
    finally:
        finish.set()
        thread.join(5)
        service.close()
    assert len(results) == 1
    assert service.adapter.closed
    with pytest.raises(ToolFailure) as error:
        service.call("get_status")
    assert error.value.code == "NOT_READY"


def test_no_raw_exception_or_nonfinite_value_reaches_client(service, monkeypatch):
    def fail():
        raise RuntimeError("API_KEY=secret")

    monkeypatch.setattr(service.adapter, "get_status", fail)
    with pytest.raises(ToolFailure) as error:
        service.call("get_status")
    assert "secret" not in str(error.value)
    monkeypatch.setattr(service.adapter, "get_status", lambda: {"data": {"score": float("nan")}})
    with pytest.raises(ToolFailure):
        service.call("get_status")


def test_all_schemas_serializable_and_forbid_extra_fields():
    for model in INPUT_MODELS.values():
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        json.dumps(schema)


def test_adapter_factory_is_lazy_and_close_without_use_does_not_initialize():
    calls = []

    def factory():
        calls.append(1)
        return Adapter()

    unused = ResearchService(adapter_factory=factory)
    unused.close()
    assert not calls
    service = ResearchService(adapter_factory=factory)
    with pytest.raises(ToolFailure):
        service.call("search_reports", {"query": ""})
    assert not calls
    service.call("get_status")
    service.call("get_status")
    service.close()
    assert calls == [1]


def test_adapter_import_failure_is_actionable_and_sanitized():
    def factory():
        raise ImportError("secret path or config")

    service = ResearchService(adapter_factory=factory)
    with pytest.raises(ToolFailure) as error:
        service.call("get_status")
    assert error.value.code == "UPSTREAM_INCOMPATIBLE"
    assert "secret" not in error.value.message
    service.close()
