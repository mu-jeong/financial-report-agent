from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core import issue_report_outbox


def _report() -> dict:
    sources = [
        {
            "role": "OBSERVED_RESULT",
            "report_uid": f"report-{index}",
            "source_sha256": f"{index:064x}",
            "chunk_uid": f"chunk-{index}",
            "chunk_sha256": f"{index + 1:064x}",
            "rank": index,
        }
        for index in range(1, 26)
    ]
    turns = [
        message
        for index in range(1, 13)
        for message in (
            {"role": "user", "content": f"user question {index}"},
            {"role": "assistant", "content": f"assistant response {index}"},
        )
    ]
    return {
        "id": "report-v3",
        "kind": "user_feedback",
        "report_target_type": "response",
        "source": "local_chat",
        "app_version": "0.6.1",
        "category": "기타",
        "comment": "답변의 수치가 근거와 다릅니다.",
        "context": {
            "remote_consent": {
                "consent_version": 1,
                "include_comment": True,
                "include_selected_question": True,
                "include_selected_answer": True,
                "include_previous_turns": True,
            },
            "selected_user_question": "최근 영업이익은 얼마인가요?",
            "selected_message": {
                "content_preview": "잘못된 답변",
                "metadata": {
                    "route": "vectordb",
                    "status": "complete",
                    "selected_sources": sources,
                },
            },
            "conversation_messages": turns,
            "trace_detail": {
                "query_rewrite": {"rewritten_query": "최근 연결 영업이익"},
                "routing": {"route": "vectordb"},
                "scope": {"search_filters": {"target_name": "삼성전자"}},
                "sources": sources,
                "answer": {"citation_ranks_used": [1]},
            },
        },
        "diagnostics": {},
    }


def test_v3_embeds_bounded_case_diagnostics_without_policy_metadata():
    remote = issue_report_outbox.build_remote_report(_report())

    assert remote["schema_version"] == 3
    assert remote["report_contract_version"] == 3
    assert remote["reported_release_id"] == "release-v0.6.1"
    diagnostics = remote["case_diagnostics"]
    assert diagnostics["schema_version"] == 1
    assert diagnostics["truncated"] is True
    assert len(diagnostics["prior_turns"]) == 8
    assert diagnostics["prior_turns"] == [
        {"role": "user", "content": f"user question {index}"}
        for index in range(5, 13)
    ]
    assert "assistant response" not in json.dumps(diagnostics)
    assert len(diagnostics["retrieval_observations"]) == 20
    assert diagnostics["retrieval_observations"][0]["role"] == "OBSERVED_RESULT"
    assert "maximum_prior_turns" not in diagnostics
    assert "maximum_retrieval_observations" not in diagnostics
    assert "unknown_field_policy" not in diagnostics
    assert "capture_policy" not in diagnostics
    assert "media_type" not in diagnostics


def test_case_diagnostics_is_absent_when_no_safe_diagnostic_content_exists():
    report = _report()
    report["context"] = {
        "remote_consent": {
            "consent_version": 1,
            "include_comment": False,
            "include_selected_question": False,
            "include_selected_answer": False,
            "include_previous_turns": False,
        }
    }

    remote = issue_report_outbox.build_remote_report(report)

    assert "case_diagnostics" not in remote


def test_case_diagnostics_requires_explicit_search_state_consent():
    report = _report()
    report["context"]["remote_consent"]["include_previous_turns"] = False

    remote = issue_report_outbox.build_remote_report(report)

    assert "case_diagnostics" not in remote
    assert remote["consent"]["include_previous_turns"] is False
    serialized = json.dumps(remote, ensure_ascii=False)
    assert "최근 연결 영업이익" not in serialized
    assert "report-1" not in serialized


def test_case_diagnostics_drops_unsafe_binary_path_and_secret_values():
    report = _report()
    report["context"]["trace_detail"]["sources"] = [
        {
            "role": "OBSERVED_RESULT",
            "report_uid": "C:\\private\\catalog.sqlite3",
            "source_sha256": "a" * 64,
            "chunk_uid": "chunk-1",
            "chunk_sha256": "b" * 64,
            "rank": 1,
        },
        {
            "role": "OBSERVED_RESULT",
            "report_uid": "Bearer abc.def.ghi",
            "source_sha256": "a" * 64,
            "rank": 2,
        },
    ]
    report["context"]["selected_message"]["metadata"]["selected_sources"] = (
        report["context"]["trace_detail"]["sources"]
    )

    remote = issue_report_outbox.build_remote_report(report)

    assert not remote["case_diagnostics"]["retrieval_observations"]
    serialized = json.dumps(remote, ensure_ascii=False)
    assert "catalog.sqlite3" not in serialized
    assert "Bearer abc.def.ghi" not in serialized


def test_native_vectordb_source_identity_survives_into_case_diagnostics():
    report = _report()
    native_source = {
        "rank": 1,
        "report_uid": "a" * 64,
        "source_sha256": "b" * 64,
        "chunk_uid": "c" * 64,
        "snapshot_id": "active-snapshot",
        "file_name": "must-not-be-exported.pdf",
    }
    report["context"]["trace_detail"]["sources"] = [native_source]
    report["context"]["selected_message"]["metadata"][
        "selected_sources"
    ] = [native_source]

    diagnostics = issue_report_outbox.build_remote_report(report)[
        "case_diagnostics"
    ]

    assert diagnostics["retrieval_observations"] == [
        {
            "role": "OBSERVED_RESULT",
            "source_uid": "a" * 64,
            "source_sha256": "b" * 64,
            "rank": 1,
        }
    ]
    assert diagnostics["evidence_refs"] == ["a" * 64]
    assert "must-not-be-exported.pdf" not in json.dumps(diagnostics)


def test_v3_envelope_remains_under_existing_128_kib_limit(tmp_path: Path):
    remote = issue_report_outbox.build_remote_report(_report())
    envelope = {
        "ingest_contract_version": 1,
        "event_id": "018f47a0-1111-7111-8111-111111111111",
        "installation_id": "018f47a0-2222-7222-8222-222222222222",
        "queued_at": "2026-08-29T00:00:00Z",
        "report": remote,
    }

    assert len(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ) <= issue_report_outbox.MAX_EVENT_BYTES


def test_ingest_validator_source_accepts_v2_and_v3_with_strict_diagnostics():
    source = Path(
        "supabase/functions/issue-report-ingest/validation.ts"
    ).read_text(encoding="utf-8")

    assert "report.schema_version === 2" in source
    assert "report.schema_version === 3" in source
    assert '"case_diagnostics"' in source
    assert "MAX_PRIOR_TURNS = 8" in source
    assert "MAX_RETRIEVAL_OBSERVATIONS = 20" in source
    assert "validateCaseDiagnostics" in source
    assert '"case_diagnostics" in report' in source
    assert "consent.include_previous_turns" in source
    assert "unknown_field" in source
    for forbidden in (".sqlite", ".faiss", ".zip", "base64"):
        assert forbidden in source


def test_v2_pending_outbox_payload_remains_a_supported_server_contract():
    source = Path(
        "supabase/functions/issue-report-ingest/validation.ts"
    ).read_text(encoding="utf-8")

    assert "supportedReportV2" in source
    assert "supportedReportV3" in source
