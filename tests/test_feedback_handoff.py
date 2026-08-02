from __future__ import annotations

import copy
import json
import stat
from pathlib import Path

import pytest

from src.core import artifact_io
from src.core.artifact_io import contains_sensitive_identifier_pattern
from src.core.feedback_handoff import (
    FeedbackHandoffError,
    build_codex_handoff_payload,
    discover_candidate_orphan_handoffs,
    list_codex_handoff_artifacts,
    load_codex_handoff,
    redact_handoff_text,
    repair_codex_handoff_markdown,
    render_codex_handoff_markdown,
    validate_codex_handoff_payload,
    write_codex_handoff,
)
from src.core.monitoring import (
    canonicalize_regression_candidate,
    compute_evaluation_run_hash,
)


def _candidate_and_run(tmp_path: Path) -> tuple[dict, dict]:
    candidate = canonicalize_regression_candidate(
        {
            "id": "candidate_r2",
            "triage_status": "reproduced",
            "contract_revision": 2,
            "severity": "S3",
            "impact_area": "retrieval_source",
            "impact_summary": "잘못된 출처 때문에 답변 근거를 신뢰하기 어렵습니다.",
            "verification_type": "graph_contract",
            "expected_approved_at": "2026-07-26T00:10:00+00:00",
            "expected_approved_by": "local_operator",
            "active_checks": ["route_pass", "source_hit"],
            "observed": {
                "reproduction_input": {"question": "NAVER 최신 리포트를 요약해줘"},
                "actual": {
                    "route": "vectordb",
                    "filters": {"target_name": "NAVER"},
                    "sources": [{"file_name": "wrong.pdf", "report_type": "company"}],
                    "state": {},
                },
            },
            "expected": {
                "route": "vectordb",
                "filters": {"target_name": "NAVER"},
                "sources": [{"file_name": "correct.pdf", "report_type": "company"}],
                "state": {},
                "manual_assertions": [],
            },
        }
    )
    run = {
        "schema_version": 2,
        "run_id": "20260726T001100Z_abcd1234",
        "run_kind": "baseline",
        "run_status": "completed",
        "created_at": "2026-07-26T00:11:00+00:00",
        "candidate_id": candidate["id"],
        "contract_revision": candidate["contract_revision"],
        "candidate_hash": candidate["candidate_hash"],
        "expected_approved_at": candidate["expected_approved_at"],
        "app_version": "0.5.1",
        "provenance": {
            "backend_mode": "synthetic_test",
            "snapshot_id": None,
            "snapshot_available": True,
            "data_revision": "unit-test-data",
            "config_fingerprint": "a" * 64,
        },
        "active_checks": ["route_pass", "source_hit"],
        "summary": {"total": 1, "passed": 0, "failed": 1},
        "results": [
            {
                "case_id": "candidate_r2",
                "status": "fail",
                "route_pass": True,
                "source_hit": False,
                "failed_checks": ["source_hit"],
            }
        ],
    }
    run["run_hash"] = compute_evaluation_run_hash(run)
    run_path = tmp_path / f"evaluation_run_{run['run_id']}.json"
    artifact_io.atomic_write_json(run_path, run)
    run["json_path"] = str(run_path)
    run["integrity_status"] = "valid"
    candidate["evidence"]["baseline_runs"] = [
        {
            "run_id": run["run_id"],
            "run_hash": run["run_hash"],
            "run_kind": "baseline",
            "status": "fail",
            "artifact_path": str(run_path),
            "candidate_id": candidate["id"],
            "contract_revision": candidate["contract_revision"],
            "candidate_hash": candidate["candidate_hash"],
            "created_at": run["created_at"],
        }
    ]
    candidate = canonicalize_regression_candidate(candidate)
    return candidate, run


@pytest.mark.parametrize(
    "unsafe",
    [
        "Bearer abcdef.123456",
        "sk-abcdefghijklmnop",
        "api_key=supersecret",
        "person@example.com",
        "010-1234-5678",
        "+82 10 1234 5678",
        r"C:\Users\alice\private.txt",
        r"C:\Users\Alice Smith\private report.txt",
        r"\\server\share\private\report.pdf",
        r"\\server\Finance Team\private report.pdf",
        "/tmp",
        "/etc",
        '"/tmp"',
        "/home/alice/private/report.pdf",
        "/home/alice smith/private report.pdf",
        "https://alice:password@example.com/a",
        "https://example.com/a?token=supersecret",
    ],
)
def test_redact_handoff_text_removes_adversarial_sensitive_values(unsafe: str):
    redacted = redact_handoff_text(f"before {unsafe} after")
    assert "[REDACTED:" in redacted
    assert unsafe not in redacted
    safety_view = redacted.replace("[REDACTED:credential]", "").replace(
        "[REDACTED:email]", ""
    ).replace("[REDACTED:phone]", "").replace("[REDACTED:path]", "").replace(
        "[REDACTED:url_credential]", ""
    ).replace(
        "[REDACTED:url_secret]", ""
    )
    assert not contains_sensitive_identifier_pattern(safety_view)


def test_plain_url_is_not_redacted_as_a_local_path():
    value = "https://example.com/report"
    assert redact_handoff_text(value) == value


@pytest.mark.parametrize(
    "raw_error",
    [
        (
            "Traceback (most recent call last):\n"
            '  File "C:\\Users\\alice\\app.py", line 7, in run\n'
            "ValueError: customer portfolio detail"
        ),
        "ValueError: customer portfolio detail",
        "metadata.error=database password lookup failed",
        "    at service.run(worker.js:42)",
    ],
)
def test_redact_handoff_text_removes_raw_error_details(raw_error: str):
    redacted = redact_handoff_text(raw_error)

    assert redacted == "[REDACTED:error_detail]"
    assert "customer portfolio detail" not in redacted
    assert "password lookup" not in redacted


def test_allowed_handoff_field_does_not_export_raw_stack(tmp_path: Path):
    candidate, run = _candidate_and_run(tmp_path)
    raw_stack = (
        "Traceback (most recent call last):\n"
        '  File "C:\\Users\\alice\\app.py", line 7, in run\n'
        "ValueError: customer portfolio detail"
    )
    candidate["impact_summary"] = raw_stack
    output = tmp_path / "handoffs"

    written = write_codex_handoff(
        candidate,
        run,
        output_dir=output,
        approved_by="local_operator",
        approval_reason="검토 완료",
    )
    manifest_text = Path(written["manifest_path"]).read_text(encoding="utf-8")
    markdown_text = Path(written["markdown_path"]).read_text(encoding="utf-8")

    assert raw_stack not in manifest_text
    assert raw_stack not in markdown_text
    assert "customer portfolio detail" not in manifest_text
    assert "customer portfolio detail" not in markdown_text
    assert "[REDACTED:error_detail]" in manifest_text
    assert "[REDACTED:error_detail]" in markdown_text


def test_single_segment_posix_paths_are_redacted_end_to_end(tmp_path: Path):
    candidate, run = _candidate_and_run(tmp_path)
    candidate["impact_summary"] = '임시 파일은 /tmp, 설정은 "/etc"에 있음'

    written = write_codex_handoff(
        candidate,
        run,
        output_dir=tmp_path / "handoffs",
        approved_by="local_operator",
        approval_reason="검토 완료",
    )
    manifest_text = Path(written["manifest_path"]).read_text(encoding="utf-8")
    markdown_text = Path(written["markdown_path"]).read_text(encoding="utf-8")

    for raw_path in ("/tmp", "/etc"):
        assert raw_path not in manifest_text
        assert raw_path not in markdown_text
    assert "[REDACTED:path]" in manifest_text
    assert "[REDACTED:path]" in markdown_text


def test_payload_is_exact_allowlist_redacted_and_uses_exact_baseline(tmp_path: Path):
    candidate, run = _candidate_and_run(tmp_path)
    candidate["impact_summary"] = (
        r"person@example.com, 010-1234-5678, C:\Users\alice\secret.txt, "
        "Bearer abcdefghijklmnop, https://x.test/a?token=abc"
    )
    candidate["observed"]["actual"]["ignored"] = "raw stack"

    payload = build_codex_handoff_payload(candidate, run)

    assert set(payload) == {
        "handoff_schema_version",
        "candidate",
        "goal",
        "user_impact",
        "provenance",
        "reproduction",
        "observed",
        "expected",
        "acceptance",
        "verification",
    }
    assert "ignored" not in json.dumps(payload, ensure_ascii=False)
    assert payload["observed"]["baseline_failed_checks"] == ["source_hit"]
    assert payload["expected"]["answer_requirements"] == []
    assert "[REDACTED:" in payload["user_impact"]["summary"]
    validate_codex_handoff_payload(payload)

    mismatched = copy.deepcopy(run)
    mismatched["run_id"] = "20260726T001101Z_abcdef12"
    with pytest.raises(FeedbackHandoffError, match="invalid_baseline"):
        build_codex_handoff_payload(candidate, mismatched)


def test_validator_rejects_unknown_nested_keys_and_unsafe_commands(tmp_path: Path):
    candidate, run = _candidate_and_run(tmp_path)
    payload = build_codex_handoff_payload(candidate, run)
    payload["expected"]["filters"]["unknown"] = "x"
    with pytest.raises(FeedbackHandoffError, match="unsupported keys"):
        validate_codex_handoff_payload(payload)


@pytest.mark.parametrize(
    "unsafe_command",
    [
        "pytest -q && echo leaked",
        "pytest -q || echo leaked",
        "pytest -q & echo leaked",
        "pytest -q | Tee-Object output.txt",
        "pytest -q > output.txt",
        "pytest -q < input.txt",
        r"pytest -q `n Write-Output leaked",
        "pytest -q $(Get-Date)",
        "pytest -q ${env:TEMP}",
        "pytest -q (Get-Date)",
        "pytest -q (Write-Output injected)",
    ],
    ids=(
        "and-chain",
        "or-chain",
        "single-ampersand",
        "pipe",
        "redirect-output",
        "redirect-input",
        "powershell-escape",
        "command-substitution",
        "variable-expansion",
        "parenthesized-expression",
        "parenthesized-command",
    ),
)
def test_handoff_rejects_shell_metacharacters_in_verification_commands(
    tmp_path: Path,
    unsafe_command: str,
):
    candidate, run = _candidate_and_run(tmp_path)
    with pytest.raises(FeedbackHandoffError, match="invalid_verification_command"):
        build_codex_handoff_payload(
            candidate,
            run,
            verification_commands=[unsafe_command],
        )


def test_write_load_list_hash_and_missing_markdown_repair(tmp_path: Path):
    candidate, run = _candidate_and_run(tmp_path)
    output = tmp_path / "handoffs"

    written = write_codex_handoff(
        candidate,
        run,
        output_dir=output,
        approved_by="local_operator",
        approval_reason="redacted preview reviewed",
    )
    loaded = load_codex_handoff(written["manifest_path"], output_root=output)

    assert loaded["integrity_status"] == "valid"
    assert loaded["companion_status"] == "present"
    assert loaded["baseline_run_id"] == run["run_id"]
    assert loaded["baseline_run_hash"] == run["run_hash"]
    assert Path(loaded["markdown_path"]).read_text(encoding="utf-8") == (
        render_codex_handoff_markdown(loaded["payload"])
    )
    assert list_codex_handoff_artifacts(output)["items"][0]["handoff_id"] == (
        written["handoff_id"]
    )

    Path(loaded["markdown_path"]).unlink()
    partial = list_codex_handoff_artifacts(output)
    assert partial["items"][0]["companion_status"] == "missing"
    assert {warning["code"] for warning in partial["warnings"]} == {
        "partial_handoff"
    }
    repaired = repair_codex_handoff_markdown(
        written["manifest_path"], output_root=output
    )
    assert repaired.read_text(encoding="utf-8") == render_codex_handoff_markdown(
        loaded["payload"]
    )


def test_listing_accepts_cwd_relative_output_root(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    candidate, run = _candidate_and_run(tmp_path)
    relative_output = Path("debug") / "codex_handoffs"
    written = write_codex_handoff(
        candidate,
        run,
        output_dir=relative_output,
        approved_by="local_operator",
        approval_reason="검토 완료",
    )

    listed = list_codex_handoff_artifacts(relative_output)

    assert listed["warnings"] == []
    assert [item["handoff_id"] for item in listed["items"]] == [
        written["handoff_id"]
    ]


def test_markdown_only_partial_is_warned_and_not_repaired(tmp_path: Path):
    output = tmp_path / "handoffs"
    partial_dir = output / "candidate_r2"
    partial_dir.mkdir(parents=True)
    partial = partial_dir / "handoff_0123456789ab.md"
    partial.write_text("partial", encoding="utf-8")

    listed = list_codex_handoff_artifacts(output)

    assert listed["items"] == []
    assert listed["warnings"] == [
        {"code": "partial_handoff", "path": str(partial), "blocking": True}
    ]


def test_invalid_utf8_manifest_is_a_blocking_listing_warning(tmp_path: Path):
    output = tmp_path / "handoffs"
    candidate_dir = output / "candidate_r2"
    candidate_dir.mkdir(parents=True)
    manifest = candidate_dir / "handoff_0123456789ab.manifest.json"
    manifest.write_bytes(b"\xff")

    listed = list_codex_handoff_artifacts(output)

    assert listed["items"] == []
    assert listed["warnings"] == [
        {
            "code": "malformed_manifest",
            "path": str(manifest),
            "blocking": True,
        }
    ]


def test_manifest_write_failure_leaves_no_untrusted_markdown_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate, run = _candidate_and_run(tmp_path)
    output = tmp_path / "handoffs"

    def fail_manifest_write(*_args, **_kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(artifact_io, "atomic_write_json", fail_manifest_write)
    with pytest.raises(OSError, match="simulated interruption"):
        write_codex_handoff(
            candidate,
            run,
            output_dir=output,
            approved_by="local_operator",
            approval_reason="reviewed",
        )

    listed = list_codex_handoff_artifacts(output)
    assert listed["items"] == []
    assert listed["warnings"] == []


def test_markdown_write_failure_leaves_repairable_canonical_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate, run = _candidate_and_run(tmp_path)
    output = tmp_path / "handoffs"

    real_write_text = artifact_io.atomic_write_text

    def fail_markdown_write(path, text):
        if Path(path).suffix == ".md":
            raise OSError("simulated interruption")
        return real_write_text(path, text)

    monkeypatch.setattr(artifact_io, "atomic_write_text", fail_markdown_write)
    with pytest.raises(OSError, match="simulated interruption"):
        write_codex_handoff(
            candidate,
            run,
            output_dir=output,
            approved_by="local_operator",
            approval_reason="reviewed",
        )

    listed = list_codex_handoff_artifacts(output)
    assert len(listed["items"]) == 1
    assert listed["items"][0]["companion_status"] == "missing"
    assert listed["warnings"] == [
        {
            "code": "partial_handoff",
            "path": listed["items"][0]["manifest_path"],
            "blocking": False,
        }
    ]


def test_hash_tampering_and_path_escape_are_rejected_before_content_use(
    tmp_path: Path,
):
    candidate, run = _candidate_and_run(tmp_path)
    output = tmp_path / "handoffs"
    written = write_codex_handoff(
        candidate,
        run,
        output_dir=output,
        approved_by="local_operator",
        approval_reason="reviewed",
    )
    manifest_path = Path(written["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["approval_reason"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FeedbackHandoffError, match="handoff_hash_mismatch"):
        load_codex_handoff(manifest_path, output_root=output)

    outside = tmp_path / "outside.manifest.json"
    outside.write_text("{not json", encoding="utf-8")
    with pytest.raises(FeedbackHandoffError, match="handoff_path_escape"):
        load_codex_handoff(outside, output_root=output)


def test_orphan_discovery_requires_current_contract_and_exact_linked_baseline(
    tmp_path: Path,
):
    candidate, run = _candidate_and_run(tmp_path)
    output = tmp_path / "handoffs"
    written = write_codex_handoff(
        candidate,
        run,
        output_dir=output,
        approved_by="local_operator",
        approval_reason="reviewed",
    )

    discovered = discover_candidate_orphan_handoffs(candidate, output_dir=output)
    assert [item["handoff_id"] for item in discovered["attachable"]] == [
        written["handoff_id"]
    ]
    assert discovered["stale"] == []

    linked = copy.deepcopy(candidate)
    linked["handoffs"] = [{"manifest_sha256": written["manifest_sha256"]}]
    assert discover_candidate_orphan_handoffs(linked, output_dir=output)[
        "attachable"
    ] == []

    stale = copy.deepcopy(candidate)
    stale["contract_revision"] += 1
    stale = canonicalize_regression_candidate(stale)
    assert discover_candidate_orphan_handoffs(stale, output_dir=output)["stale"]


def test_manifest_symlink_is_rejected_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate, run = _candidate_and_run(tmp_path)
    output = tmp_path / "handoffs"
    written = write_codex_handoff(
        candidate,
        run,
        output_dir=output,
        approved_by="local_operator",
        approval_reason="reviewed",
    )
    original = Path(written["manifest_path"])
    link = original.with_name("handoff_0123456789ab.manifest.json")
    real_lstat = Path.lstat

    def symlink_lstat(path: Path):
        if path == link:
            return type("SymlinkStat", (), {"st_mode": stat.S_IFLNK})()
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", symlink_lstat)
    with pytest.raises(FeedbackHandoffError, match="handoff_symlink"):
        load_codex_handoff(link, output_root=output)
