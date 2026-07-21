"""Operator CLI for copied-install V1 assessment and zero-embedding conversion."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.migrations.v2.assess import ProvenanceEvidence, assess_v1_install
from src.migrations.v2.evidence import (
    create_copied_v1_install,
    seal_compatibility_bundle,
    validate_compatibility_bundle,
)
from src.migrations.v2.import_v1 import (
    ConversionResult,
    convert_v1_seed,
    validate_converted_seed,
)
from src.retrieval.identity import EmbeddingProfile, canonical_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run explicit, off-path V2 native retrieval migration stages"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    copy_parser = subparsers.add_parser("copy", help="capture an immutable copied V1 install")
    copy_parser.add_argument("--source-root", type=Path, required=True)
    copy_parser.add_argument("--destination-root", type=Path, required=True)
    copy_parser.add_argument("--output", type=Path, required=True)

    assess_parser = subparsers.add_parser("assess", help="assess trusted copied V1 artifacts")
    assess_parser.add_argument("--copied-root", type=Path, required=True)
    assess_parser.add_argument("--expected-hashes", type=Path, required=True)
    assess_parser.add_argument("--provenance", type=Path)
    assess_parser.add_argument("--output", type=Path, required=True)

    seal_parser = subparsers.add_parser("seal", help="seal the epoch-zero compatibility bundle")
    seal_parser.add_argument("--copied-root", type=Path, required=True)
    seal_parser.add_argument("--data-root", type=Path, required=True)
    seal_parser.add_argument("--output", type=Path, required=True)

    convert_parser = subparsers.add_parser(
        "convert", help="convert existing vectors into an epoch-zero native seed"
    )
    convert_parser.add_argument("--copied-root", type=Path, required=True)
    convert_parser.add_argument("--data-root", type=Path, required=True)
    convert_parser.add_argument("--expected-hashes", type=Path, required=True)
    convert_parser.add_argument("--source-hashes", type=Path, required=True)
    convert_parser.add_argument("--profile", type=Path, required=True)
    convert_parser.add_argument("--bundle-id", required=True)
    convert_parser.add_argument("--canonical-paths", type=Path)
    convert_parser.add_argument("--provenance", type=Path)
    convert_parser.add_argument("--output", type=Path, required=True)

    validate_parser = subparsers.add_parser("validate", help="revalidate a converted seed")
    validate_parser.add_argument("--data-root", type=Path, required=True)
    validate_parser.add_argument("--conversion-result", type=Path, required=True)
    validate_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "copy":
        evidence = create_copied_v1_install(args.source_root, args.destination_root)
        payload = {
            "schema_version": 1,
            "kind": "v2_copied_install_result",
            "evidence": evidence.to_dict(),
        }
    elif args.command == "assess":
        assessment = assess_v1_install(
            args.copied_root,
            expected_hashes=_string_mapping(args.expected_hashes, "expected hashes"),
            provenance=_provenance(args.provenance),
        )
        payload = {
            "schema_version": 1,
            "kind": "v2_assessment_result",
            "assessment_digest": assessment.digest,
            "assessment": assessment.to_dict(),
        }
    elif args.command == "seal":
        bundle = seal_compatibility_bundle(args.copied_root, args.data_root)
        payload = {
            "schema_version": 1,
            "kind": "v2_compatibility_bundle_result",
            "bundle": bundle.to_dict(),
            "manifest_sha256": bundle.manifest_sha256,
        }
    elif args.command == "convert":
        result = convert_v1_seed(
            args.copied_root,
            args.data_root,
            expected_hashes=_string_mapping(args.expected_hashes, "expected hashes"),
            profile=EmbeddingProfile.from_mapping(_json_object(args.profile)),
            source_hashes=_string_mapping(args.source_hashes, "source hashes"),
            compatibility_bundle_id=args.bundle_id,
            canonical_relative_paths=(
                None
                if args.canonical_paths is None
                else _string_mapping(args.canonical_paths, "canonical paths")
            ),
            provenance=_provenance(args.provenance),
        )
        payload = {
            "schema_version": 1,
            "kind": "v2_conversion_result",
            "result": asdict(result),
        }
    else:
        raw = _json_object(args.conversion_result)
        if set(raw) == {"schema_version", "kind", "result"}:
            raw = raw["result"]
        result = ConversionResult(**raw)
        validate_converted_seed(args.data_root, result)
        payload = {
            "schema_version": 1,
            "kind": "v2_conversion_validation",
            "snapshot_id": result.snapshot_id,
            "snapshot_sha256": result.snapshot_sha256,
            "valid": True,
        }

    _write_immutable_json(args.output, payload)
    print(
        json.dumps(
            {"status": "ok", "stage": args.command, "evidence": args.output.name},
            ensure_ascii=False,
        )
    )
    return 0


def _provenance(path: Path | None) -> ProvenanceEvidence | None:
    if path is None:
        return None
    value = _json_object(path)
    allowed = set(ProvenanceEvidence.__dataclass_fields__)
    if set(value) - allowed:
        raise ValueError("provenance contains unsupported fields")
    return ProvenanceEvidence(**value)


def _string_mapping(path: Path, label: str) -> dict[str, str]:
    value = _json_object(path)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        raise ValueError(f"{label} must map strings to strings")
    return dict(value)


def _json_object(path: Path) -> dict[str, Any]:
    source = path.resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise ValueError("migration JSON input must be a regular local file")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("migration JSON input is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("migration JSON input must be an object")
    return value


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"migration evidence already exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.parent / f".migration-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
