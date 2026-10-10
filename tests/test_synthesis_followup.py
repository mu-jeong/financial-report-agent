from copy import deepcopy

import pytest

from apps.gui.chat_jobs import SynthesisScopeChangedError, _validate_synthesis_followup


def _scope():
    return {"scope_source": "report_synthesis", "document_uids": ["original-version"],
            "file_names": ["report.pdf"], "search_filters": {"target_name": "Alpha",
            "report_type": "company", "report_date_start": "2026-07-01",
            "report_date_end": "2026-07-31", "file_names": ["report.pdf"]}}


def test_inherited_followup_requires_original_document_uid_even_when_filename_matches():
    scope = _scope()
    state = {"scope_source": "prior_search_scope", "search_filters": deepcopy(scope["search_filters"])}
    assert _validate_synthesis_followup(scope, state, [{"report_uid": "original-version"}])
    with pytest.raises(SynthesisScopeChangedError, match="현재"):
        _validate_synthesis_followup(scope, state, [{"report_uid": "replacement-version", "file_name": "report.pdf"}])


def test_real_rdb_source_identity_matches_synthesis_version(monkeypatch):
    from src.nodes import rdb

    scope = _scope()
    state = {"scope_source": "prior_search_scope", "search_filters": deepcopy(scope["search_filters"])}
    identity = {"source_uid": "original-version", "source_sha256": "original-hash"}
    monkeypatch.setattr(rdb, "_report_identities_by_file_name", lambda _files: {"report.pdf": identity})
    sources = rdb.extract_sources_from_rdb_result({"columns": ["file_name"], "rows": [["report.pdf"]]})
    assert sources and sources[0]["source_uid"] == "original-version"
    assert _validate_synthesis_followup(scope, state, sources)
    identity["source_uid"] = "replacement-version"
    sources = rdb.extract_sources_from_rdb_result({"columns": ["file_name"], "rows": [["report.pdf"]]})
    with pytest.raises(SynthesisScopeChangedError):
        _validate_synthesis_followup(scope, state, sources)


def test_explicit_company_or_date_change_can_start_a_new_scope():
    scope = _scope()
    for key, value in [("target_name", "Beta"), ("report_date_end", "2026-08-31")]:
        filters = deepcopy(scope["search_filters"])
        filters[key] = value
        state = {"scope_source": "prior_search_scope", "search_filters": filters}
        assert not _validate_synthesis_followup(scope, state, [{"report_uid": "other"}])


def test_ordinary_chat_scope_is_unchanged():
    scope = _scope()
    scope["scope_source"] = "current_turn"
    assert not _validate_synthesis_followup(scope, {}, [{"report_uid": "other"}])


def test_document_ordinal_or_section_narrowing_still_validates_document_versions():
    scope = _scope()
    scope["file_names"].append("another.pdf")
    for files in [["report.pdf"], []]:
        filters = deepcopy(scope["search_filters"])
        filters["file_names"] = files
        state = {"scope_source": "prior_search_scope", "search_filters": filters}
        assert _validate_synthesis_followup(scope, state, [{"report_uid": "original-version"}])
        with pytest.raises(SynthesisScopeChangedError):
            _validate_synthesis_followup(scope, state, [{"report_uid": "replacement-version"}])
