from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.core import artifact_io


def test_atomic_write_json_replaces_target_with_pretty_utf8_payload(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "artifact.json"
    payload = {"name": "한글", "nested": {"answer": 42}}

    result = artifact_io.atomic_write_json(target, payload)

    assert result == target
    written = target.read_text(encoding="utf-8")
    assert json.loads(written) == payload
    assert written == json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    assert b"\r\n" not in target.read_bytes()
    assert not list(target.parent.glob("*.tmp"))


def test_atomic_write_json_rejects_non_finite_values_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.json"
    target.write_text('{"original": true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Out of range float values"):
        artifact_io.atomic_write_json(target, {"value": float("nan")})

    assert target.read_text(encoding="utf-8") == '{"original": true}\n'
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_strict_json_loads_rejects_non_standard_constants(constant: str) -> None:
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        artifact_io.strict_json_loads(f'{{"value": {constant}}}')


def test_atomic_write_text_replaces_target_without_partial_content(tmp_path: Path) -> None:
    target = tmp_path / "artifact.txt"
    target.write_text("old", encoding="utf-8")

    result = artifact_io.atomic_write_text(target, "새로운 내용\n")

    assert result == target
    assert target.read_text(encoding="utf-8") == "새로운 내용\n"
    assert target.read_bytes().endswith(b"\n")
    assert b"\r\n" not in target.read_bytes()
    assert not list(tmp_path.glob("*.tmp"))


def test_atomic_write_text_flushes_and_fsyncs_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync = artifact_io.os.fsync
    real_replace = artifact_io.os.replace

    def recording_fsync(file_descriptor: int) -> None:
        events.append("fsync")
        real_fsync(file_descriptor)

    def recording_replace(source: str | Path, target: str | Path) -> None:
        events.append("replace")
        real_replace(source, target)

    monkeypatch.setattr(artifact_io.os, "fsync", recording_fsync)
    monkeypatch.setattr(artifact_io.os, "replace", recording_replace)

    artifact_io.atomic_write_text(tmp_path / "artifact.txt", "content")

    assert events == ["fsync", "replace"]


def test_atomic_write_json_replace_failure_keeps_original_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact.json"
    original = b'{"known":"bytes"}\n'
    target.write_bytes(original)

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(artifact_io.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        artifact_io.atomic_write_json(target, {"replacement": True})

    assert target.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "value",
    [
        "sk-secret-token",
        "Bearer abc.def.ghi",
        "api_key=top-secret",
        "user@example.com",
        "010-1234-5678",
        "+82 10 1234 5678",
        r"C:\Users\name\case",
        r"\\server\share\case",
        "/tmp",
        "/etc",
        '"/tmp"',
        "/home/name/case",
        "https://user:password@example.com/report",
        "https://example.com/report?access_token=secret",
    ],
)
def test_contains_sensitive_identifier_pattern_detects_contract_patterns(value: str) -> None:
    assert artifact_io.contains_sensitive_identifier_pattern(value)


def test_plain_url_is_not_misclassified_as_a_local_posix_path() -> None:
    assert not artifact_io.contains_sensitive_identifier_pattern(
        "https://example.com/report"
    )


@pytest.mark.parametrize(
    "value",
    ["candidate_123", "run-20260726.1", "A", "a" * 128],
)
def test_safe_artifact_identifier_accepts_only_safe_opaque_values(value: str) -> None:
    assert artifact_io.is_safe_artifact_identifier(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".hidden",
        "a" * 129,
        "../escape",
        "candidate/escape",
        "user@example.com",
        "010-1234-5678",
        "sk-secret-token",
        r"C:\Users\name\case",
    ],
)
def test_safe_artifact_identifier_rejects_invalid_or_sensitive_values(value: str) -> None:
    assert not artifact_io.is_safe_artifact_identifier(value)


@pytest.mark.parametrize(
    "value",
    [
        "../escape",
        "user@example.com",
        "010-1234-5678",
        "sk-secret-token",
        r"C:\Users\name\case",
    ],
)
def test_safe_artifact_token_hashes_unsafe_values_deterministically(value: str) -> None:
    expected = f"id_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"

    assert artifact_io.safe_artifact_token(value) == expected
    assert artifact_io.safe_artifact_token(value) == expected
    assert value not in expected


def test_safe_artifact_token_preserves_safe_identifier() -> None:
    assert artifact_io.safe_artifact_token("candidate_123") == "candidate_123"
