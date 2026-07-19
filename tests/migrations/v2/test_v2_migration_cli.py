from __future__ import annotations

import hashlib
import json

from scripts.migrations.v2.migrate_v2_native import main
from tests.retrieval.test_retrieval_build_service import _legacy_install, _profile


def test_operator_cli_assesses_seals_converts_and_revalidates_without_embedding(
    tmp_path,
):
    copied = tmp_path / "copied-v1"
    copied.mkdir()
    expected = _legacy_install(copied)
    before = {
        relative: hashlib.sha256((copied / relative).read_bytes()).hexdigest()
        for relative in expected
    }
    expected_path = tmp_path / "expected-hashes.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    source_hashes_path = tmp_path / "source-hashes.json"
    source_hashes_path.write_text(
        json.dumps({"a.pdf": "11" * 32, "b.pdf": "22" * 32}),
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(_profile().to_dict()), encoding="utf-8")
    assessment_path = tmp_path / "assessment.json"
    bundle_path = tmp_path / "bundle.json"
    conversion_path = tmp_path / "conversion.json"
    validation_path = tmp_path / "validation.json"
    data_root = tmp_path / "데이터 root"

    assert main(
        [
            "assess",
            "--copied-root",
            str(copied),
            "--expected-hashes",
            str(expected_path),
            "--output",
            str(assessment_path),
        ]
    ) == 0
    assert main(
        [
            "seal",
            "--copied-root",
            str(copied),
            "--data-root",
            str(data_root),
            "--output",
            str(bundle_path),
        ]
    ) == 0
    bundle_id = json.loads(bundle_path.read_text(encoding="utf-8"))["bundle"][
        "bundle_id"
    ]
    assert main(
        [
            "convert",
            "--copied-root",
            str(copied),
            "--data-root",
            str(data_root),
            "--expected-hashes",
            str(expected_path),
            "--source-hashes",
            str(source_hashes_path),
            "--profile",
            str(profile_path),
            "--bundle-id",
            bundle_id,
            "--output",
            str(conversion_path),
        ]
    ) == 0
    assert main(
        [
            "validate",
            "--data-root",
            str(data_root),
            "--conversion-result",
            str(conversion_path),
            "--output",
            str(validation_path),
        ]
    ) == 0

    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    after = {
        relative: hashlib.sha256((copied / relative).read_bytes()).hexdigest()
        for relative in expected
    }
    assert assessment["assessment"]["observable"]["ntotal"] == 2
    assert validation["valid"] is True
    assert before == after
