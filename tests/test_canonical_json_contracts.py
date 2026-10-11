from __future__ import annotations

import pytest

from src.core import (
    artifact_io,
    feedback_handoff,
    monitoring,
    release_assets,
    reproduction_manifest,
)


def test_domain_wrappers_share_canonical_bytes_without_changing_output() -> None:
    payload = {"z": [3, 1], "a": "한글", "nested": {"b": False}}
    expected = '{"a":"한글","nested":{"b":false},"z":[3,1]}'.encode("utf-8")

    assert artifact_io.canonical_json_bytes(payload) == expected
    assert release_assets._canonical_json_bytes(payload) == expected
    assert reproduction_manifest._canonical_bytes(payload) == expected
    assert monitoring._canonical_json_bytes(payload) == expected
    assert feedback_handoff._canonical_json_bytes(payload) == expected


def test_release_wrapper_preserves_json_serialization_error() -> None:
    with pytest.raises(TypeError):
        release_assets._canonical_json_bytes({"unsupported": {1, 2}})


@pytest.mark.parametrize("value", [float("nan"), {1, 2}])
def test_reproduction_wrapper_preserves_domain_error(value: object) -> None:
    with pytest.raises(
        reproduction_manifest.ReproductionManifestError,
        match="reproduction manifest contains unsupported values",
    ):
        reproduction_manifest._canonical_bytes({"unsupported": value})


@pytest.mark.parametrize("value", [float("nan"), {1, 2}])
def test_handoff_wrapper_preserves_json_validation_error(value: object) -> None:
    with pytest.raises(feedback_handoff.FeedbackHandoffError) as raised:
        feedback_handoff._canonical_json_bytes({"unsupported": value})
    assert raised.value.code == "invalid_payload"
    assert str(raised.value) == (
        "invalid_payload: non-finite or unsupported JSON value"
    )


def test_utf8_encoding_errors_preserve_domain_boundaries() -> None:
    payload = {"unpaired_surrogate": "\ud800"}
    for serializer in (
        artifact_io.canonical_json_bytes,
        release_assets._canonical_json_bytes,
        monitoring._canonical_json_bytes,
        feedback_handoff._canonical_json_bytes,
    ):
        with pytest.raises(UnicodeEncodeError):
            serializer(payload)

    with pytest.raises(reproduction_manifest.ReproductionManifestError):
        reproduction_manifest._canonical_bytes(payload)
