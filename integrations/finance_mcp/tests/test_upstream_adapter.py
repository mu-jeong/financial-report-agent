from __future__ import annotations

import pytest

from integrations.finance_mcp.contracts import ToolFailure
from integrations.finance_mcp import upstream_adapter as adapter_module
from integrations.finance_mcp.tests.upstream_fixture import (
    FakeEmbeddings,
    create_empty_fixture,
    create_native_fixture,
    digest,
    forbid_snapshot_load,
    insert_inactive_parent,
    publish_delete,
    publish_replacement,
    response_with_profile,
)
from integrations.finance_mcp.upstream_adapter import UpstreamAdapter


def test_clean_missing_root_reports_not_ready(tmp_path):
    adapter = UpstreamAdapter(tmp_path)
    result = adapter.get_status()

    assert result["revision"] is None
    assert result["data"]["state"] == "not_ready"
    assert result["data"]["report_count"] == 0
    assert not any(result["data"]["capabilities"].values())


def test_list_and_stats_read_the_active_native_view(tmp_path):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)
    try:
        listed = adapter.list_reports(
            {"target_names": ["Alpha"], "report_types": ["company"]},
            limit=10,
            offset=0,
            expected_revision=None,
        )
        stats = adapter.get_report_stats(
            {}, "broker", limit=10, offset=0, expected_revision=listed["revision"]
        )
        empty = adapter.list_reports(
            {"target_names": []}, 10, 0, listed["revision"]
        )
    finally:
        adapter.close()

    assert listed["data"]["total_count"] == 1
    assert listed["data"]["reports"][0]["title"] == "Alpha first"
    assert stats["data"]["total_count"] == 3
    assert stats["data"]["groups"] == [
        {"value": "Broker A", "report_count": 2},
        {"value": "Broker B", "report_count": 1},
    ]
    assert empty["data"] == {"reports": [], "total_count": 0}


def test_list_pagination_is_revision_pinned(tmp_path):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)
    try:
        first = adapter.list_reports({}, limit=1, offset=0, expected_revision=None)
        second = adapter.list_reports(
            {}, limit=1, offset=1, expected_revision=first["revision"]
        )
    finally:
        adapter.close()

    assert first["next_offset"] == 1
    assert second["revision"] == first["revision"]
    assert first["data"]["reports"][0]["report_uid"] != second["data"]["reports"][0]["report_uid"]


def test_read_report_is_bounded_and_rejects_stale_revision(tmp_path):
    fixture = create_native_fixture(tmp_path)
    insert_inactive_parent(fixture, report_id=1, body="must stay invisible")
    adapter = UpstreamAdapter(fixture.data_root)
    report_uid = digest("report-1")
    try:
        initial = adapter.list_reports({}, 10, 0, None)
        first = adapter.read_report(report_uid, initial["revision"], 0, 7)
        second = adapter.read_report(
            report_uid, initial["revision"], first["next_offset"], 100
        )
        publish_replacement(
            fixture,
            path=str(fixture.rows[0]["path"]),
            body="replacement body",
        )
        current = adapter.list_reports({}, 10, 0, None)
        with pytest.raises(ToolFailure) as caught:
            adapter.read_report(report_uid, initial["revision"], 0, 100)
    finally:
        adapter.close()

    assert first["data"]["content"] == "alpha f"
    assert first["next_offset"] == 7
    assert second["data"]["content"] == "irst indexed body"
    assert "invisible" not in second["data"]["content"]
    assert second["next_offset"] is None
    assert current["data"]["total_count"] == initial["data"]["total_count"]
    assert current["revision"] != initial["revision"]
    assert caught.value.code == "STALE_REVISION"


def test_delta_replacement_is_active_for_metadata_body_and_vector_search(tmp_path):
    fixture = create_native_fixture(tmp_path)
    replacement_uid = publish_replacement(
        fixture,
        path=str(fixture.rows[0]["path"]),
        body="replacement body",
    )
    adapter = UpstreamAdapter(fixture.data_root)
    adapter._embeddings = FakeEmbeddings()  # provider seam; Native V2 remains real
    try:
        listed = adapter.list_reports({"target_names": ["Alpha"]}, 10, 0, None)
        searched = adapter.search_reports("alpha", {"target_names": ["Alpha"]}, 3)
        read = adapter.read_report(replacement_uid, searched["revision"], 0, 100)
    finally:
        adapter.close()

    listed_uids = {item["report_uid"] for item in listed["data"]["reports"]}
    assert replacement_uid in listed_uids
    assert digest("report-1") not in listed_uids
    assert searched["revision"] == listed["revision"]
    assert searched["data"]["results"][0]["report_uid"] == replacement_uid
    assert searched["data"]["results"][0]["excerpt"] == "replacement body"
    assert read["data"]["content"] == "replacement body"


def test_provider_failure_does_not_expose_provider_text(tmp_path):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)

    class FailingEmbeddings:
        def embed_query(self, _query: str):
            raise RuntimeError("secret provider response")

    adapter._embeddings = FailingEmbeddings()
    try:
        with pytest.raises(ToolFailure) as caught:
            adapter.search_reports("query", {}, 1)
    finally:
        adapter.close()

    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert "secret" not in caught.value.message


def test_delta_only_report_is_visible_and_deleted_base_report_is_absent(tmp_path):
    fixture = create_native_fixture(tmp_path)
    delta_uid = publish_replacement(
        fixture,
        path="reports/delta-only.pdf",
        body="delta only body",
        sequence=1,
    )
    deleted_uid = digest("report-1")
    publish_delete(
        fixture,
        path=str(fixture.rows[0]["path"]),
        sequence=2,
    )
    adapter = UpstreamAdapter(fixture.data_root)
    adapter._embeddings = FakeEmbeddings()
    try:
        listed = adapter.list_reports({"target_names": ["Alpha"]}, 10, 0, None)
        searched = adapter.search_reports("delta", {"target_names": ["Alpha"]}, 3)
        read = adapter.read_report(delta_uid, searched["revision"], 0, 100)
        with pytest.raises(ToolFailure) as caught:
            adapter.read_report(deleted_uid, searched["revision"], 0, 100)
    finally:
        adapter.close()

    listed_uids = {item["report_uid"] for item in listed["data"]["reports"]}
    assert delta_uid in listed_uids
    assert deleted_uid not in listed_uids
    assert searched["data"]["results"][0]["report_uid"] == delta_uid
    assert read["data"]["content"] == "delta only body"
    assert caught.value.code == "REPORT_NOT_FOUND"


def test_initialized_empty_corpus_returns_empty_without_embedding(tmp_path):
    data_root = create_empty_fixture(tmp_path)
    adapter = UpstreamAdapter(data_root)

    class ForbiddenEmbeddings:
        def embed_query(self, _query: str):
            raise AssertionError("empty corpus must not call provider")

    adapter._embeddings = ForbiddenEmbeddings()
    try:
        status = adapter.get_status()
        listed = adapter.list_reports({}, 10, 0, None)
        stats = adapter.get_report_stats({}, None, 10, 0, None)
        searched = adapter.search_reports("query", {}, 3)
    finally:
        adapter.close()

    assert status["data"]["state"] == "empty"
    assert listed["data"] == {"reports": [], "total_count": 0}
    assert stats["data"]["total_count"] == 0
    assert searched["data"]["strategy"] == "empty"
    assert searched["data"]["results"] == []


def test_provably_empty_filter_does_not_call_embedding_provider(tmp_path):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)

    class ForbiddenEmbeddings:
        def embed_query(self, _query: str):
            raise AssertionError("empty scope must not call provider")

    adapter._embeddings = ForbiddenEmbeddings()
    try:
        result = adapter.search_reports("query", {"target_names": []}, 3)
    finally:
        adapter.close()

    assert result["data"]["strategy"] == "empty"
    assert result["revision"] is not None


def test_unmatched_filter_does_not_call_embedding_provider(tmp_path):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)

    class ForbiddenEmbeddings:
        def embed_query(self, _query: str):
            raise AssertionError("unmatched scope must not call provider")

    adapter._embeddings = ForbiddenEmbeddings()
    try:
        result = adapter.search_reports("query", {"target_names": ["Missing"]}, 3)
    finally:
        adapter.close()

    assert result["data"]["strategy"] == "empty"
    assert result["data"]["results"] == []
    assert result["revision"] is not None


def test_nonfinite_provider_vector_is_rejected_before_native_search(tmp_path):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)

    class NonfiniteEmbeddings:
        def embed_query(self, _query: str):
            return [float("nan"), 5.0]

    adapter._embeddings = NonfiniteEmbeddings()
    try:
        with pytest.raises(ToolFailure) as caught:
            adapter.search_reports("query", {}, 1)
    finally:
        adapter.close()

    assert caught.value.code == "PROVIDER_UNAVAILABLE"


@pytest.mark.parametrize("provider_error", [IndexError, AttributeError])
def test_malformed_successful_provider_payload_is_provider_failure(tmp_path, provider_error):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)

    class MalformedEmbeddings:
        def embed_query(self, _query: str):
            raise provider_error("HTTP 200 response contained no embedding rows")

    adapter._embeddings = MalformedEmbeddings()
    try:
        with pytest.raises(ToolFailure) as caught:
            adapter.search_reports("query", {}, 1)
    finally:
        adapter.close()

    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert "embedding rows" not in caught.value.message


def test_profile_change_between_embedding_and_search_is_stale(tmp_path):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)
    adapter._embeddings = FakeEmbeddings()
    real_reader = adapter._native_reader(fixture.catalog)

    class RacingReader:
        def search(self, *args, **kwargs):
            return response_with_profile(
                real_reader.search(*args, **kwargs),
                "profile-published-during-provider-call",
            )

    adapter._reader = RacingReader()
    try:
        with pytest.raises(ToolFailure) as caught:
            adapter.search_reports("query", {}, 1)
    finally:
        adapter.close()

    assert caught.value.code == "STALE_REVISION"


def test_status_search_capability_requires_api_key(tmp_path, monkeypatch):
    fixture = create_native_fixture(tmp_path)
    monkeypatch.setattr(adapter_module, "OPENROUTER_API_KEY", "")
    adapter = UpstreamAdapter(fixture.data_root)
    try:
        status = adapter.get_status()
    finally:
        adapter.close()

    assert status["data"]["capabilities"]["list_reports"] is True
    assert status["data"]["capabilities"]["search_reports"] is False


def test_read_report_pages_across_active_parent_separator(tmp_path):
    fixture = create_native_fixture(tmp_path, split_first_report=True)
    adapter = UpstreamAdapter(fixture.data_root)
    report_uid = digest("report-1")
    pages: list[str] = []
    offset = 0
    try:
        revision = adapter.list_reports({}, 10, 0, None)["revision"]
        while True:
            page = adapter.read_report(report_uid, revision, offset, 5)
            pages.append(page["data"]["content"])
            assert len(page["data"]["content"]) <= 5
            if page["next_offset"] is None:
                assert page["data"]["offset"] == offset
                break
            offset = page["next_offset"]
    finally:
        adapter.close()

    assert "".join(pages) == "alpha first \n\nindexed body"


def test_metadata_tools_do_not_materialize_vector_snapshot(tmp_path, monkeypatch):
    fixture = create_native_fixture(tmp_path)
    forbid_snapshot_load(monkeypatch)
    adapter = UpstreamAdapter(fixture.data_root)
    try:
        status = adapter.get_status()
        listed = adapter.list_reports({}, 10, 0, None)
        stats = adapter.get_report_stats({}, None, 10, 0, None)
        read = adapter.read_report(digest("report-1"), listed["revision"], 0, 10)
    finally:
        adapter.close()

    assert status["data"]["snapshot_validated"] is False
    assert listed["data"]["total_count"] == 3
    assert stats["data"]["total_count"] == 3
    assert read["data"]["content"] == "alpha firs"


@pytest.mark.parametrize("failure", [ValueError("index mismatch"), TypeError("API changed")])
def test_native_reader_contract_failures_are_explicit(tmp_path, failure):
    fixture = create_native_fixture(tmp_path)
    adapter = UpstreamAdapter(fixture.data_root)
    adapter._embeddings = FakeEmbeddings()

    class BrokenReader:
        def search(self, *_args, **_kwargs):
            raise failure

    adapter._reader = BrokenReader()
    try:
        with pytest.raises(ToolFailure) as caught:
            adapter.search_reports("query", {}, 1)
    finally:
        adapter.close()

    assert caught.value.code == "UPSTREAM_INCOMPATIBLE"
    assert "mismatch" not in caught.value.message
    assert "changed" not in caught.value.message
