"""Create and validate the natural-language Gate D release query artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.retrieval.identity import canonical_json
from src.migrations.v2.validation.installed_probe import run_probe


class ReleaseQueryError(RuntimeError):
    """Raised when a Gate D query artifact cannot be proved."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create one immutable natural-language V2 release query"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--query-id", required=True)
    parser.add_argument("--query-text", required=True)
    parser.add_argument("--expected-report-uid", required=True)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve(strict=False)
    try:
        root = _safe_directory(args.data_root)
        if output.exists() or output.is_symlink():
            raise ReleaseQueryError("query output path must be new")
        contract = _active_query_contract(root, args.expected_report_uid)
        from src.configs.config import (
            OPENROUTER_API_KEY,
            OPENROUTER_APP_TITLE,
            OPENROUTER_APP_URL,
            OPENROUTER_DATA_COLLECTION,
        )
        from src.llms.embeddings import OpenRouterEmbeddings

        embeddings = OpenRouterEmbeddings(
            model=contract["model"],
            api_key=OPENROUTER_API_KEY or "",
            app_url=OPENROUTER_APP_URL,
            app_title=OPENROUTER_APP_TITLE,
            data_collection=OPENROUTER_DATA_COLLECTION,
        )
        payload = create_query_spec(
            root,
            query_id=args.query_id,
            query_text=args.query_text,
            expected_report_uid=args.expected_report_uid,
            k=args.k,
            embed_query=embeddings.embed_query,
        )
        _write_immutable_json(output, payload)
    except (OSError, ValueError, sqlite3.Error, ReleaseQueryError) as exc:
        print(
            json.dumps(
                {"status": "failed", "error": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "kind": payload["kind"],
                "query_id": payload["query_id"],
                "dimension": len(payload["vector"]),
                "query_spec_sha256": _sha256_file(output),
            },
            ensure_ascii=False,
        )
    )
    return 0


def create_query_spec(
    data_root: str | Path,
    *,
    query_id: str,
    query_text: str,
    expected_report_uid: str,
    k: int,
    embed_query: Callable[[str], list[float]],
) -> dict[str, Any]:
    root = _safe_directory(data_root)
    if not isinstance(query_id, str) or not query_id.strip():
        raise ReleaseQueryError("query ID is invalid")
    if not isinstance(query_text, str) or not query_text.strip():
        raise ReleaseQueryError("query text is invalid")
    if not _is_sha256(expected_report_uid):
        raise ReleaseQueryError("expected report UID is invalid")
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ReleaseQueryError("query k is invalid")
    contract = _active_query_contract(root, expected_report_uid)
    raw_vector = embed_query(query_text)
    if not isinstance(raw_vector, list) or len(raw_vector) != contract["dimension"]:
        raise ReleaseQueryError("query embedding dimension is invalid")
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        for value in raw_vector
    ):
        raise ReleaseQueryError("query embedding contains an invalid value")
    vector = [float(value) for value in raw_vector]
    query_text_sha256 = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
    vector_sha256 = hashlib.sha256(
        np.asarray(vector, dtype=np.float32).tobytes()
    ).hexdigest()
    citation = {
        field: contract[field]
        for field in (
            "canonical_relative_path",
            "report_type",
            "report_date",
            "target_name",
            "title",
            "broker",
        )
    }
    payload = {
        "schema_version": 1,
        "kind": "v2_release_semantic_query",
        "query_id": query_id.strip(),
        "query_text": query_text,
        "vector": vector,
        "embedding_attestation": {
            "provider": "openrouter",
            "model": contract["model"],
            "input_type": "search_query",
            "provider_calls": 1,
            "query_text_sha256": query_text_sha256,
            "vector_sha256": vector_sha256,
        },
        "expected_report_uid": expected_report_uid,
        "expected_citation": citation,
        "k": k,
        "scopes": {
            "unfiltered": None,
            "empty": {"empty": True},
            "narrow": {
                "target_name": contract["target_name"],
                "report_date": contract["report_date"],
            },
            "broad": {"report_type": contract["report_type"]},
            "near_universe": {
                "report_date_start": contract["minimum_report_date"],
                "report_date_end": contract["maximum_report_date"],
            },
            "prior_scope": {
                "prior_scope": {
                    "canonical_relative_path": contract[
                        "canonical_relative_path"
                    ]
                }
            },
        },
    }
    probe = run_probe(root, payload, samples=2)
    if (
        probe["gate_d_search"]["top_report_uid"] != expected_report_uid
        or probe["gate_d_search"]["top_rank"] != 1
        or probe["gate_d_search"]["citation_complete"] is not True
    ):
        raise ReleaseQueryError("query does not satisfy Gate D")
    return payload


def _active_query_contract(root: Path, expected_report_uid: str) -> dict[str, Any]:
    catalog = _catalog(root)
    connection = sqlite3.connect(
        f"file:{catalog.resolve(strict=True).as_posix()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT report.canonical_relative_path, report.report_type,
                   report.report_date, report.target_name, report.title,
                   report.broker, profile.model, snapshot.dimension
            FROM retrieval_runtime AS runtime
            JOIN vector_snapshots AS snapshot
              ON snapshot.snapshot_id = runtime.active_snapshot_id
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            JOIN snapshot_membership AS membership
              ON membership.snapshot_id = snapshot.snapshot_id
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE runtime.runtime_id = 1 AND report.report_uid = ?
            LIMIT 1
            """,
            (expected_report_uid,),
        ).fetchone()
        bounds = connection.execute(
            """
            SELECT MIN(report.report_date), MAX(report.report_date)
            FROM retrieval_runtime AS runtime
            JOIN snapshot_membership AS membership
              ON membership.snapshot_id = runtime.active_snapshot_id
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE runtime.runtime_id = 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None or bounds is None or bounds[0] is None or bounds[1] is None:
        raise ReleaseQueryError("expected report is not in the active snapshot")
    result = dict(row)
    result["minimum_report_date"] = str(bounds[0])
    result["maximum_report_date"] = str(bounds[1])
    if any(
        not isinstance(result.get(field), str) or not result[field].strip()
        for field in (
            "canonical_relative_path",
            "report_type",
            "report_date",
            "target_name",
            "title",
            "broker",
            "model",
        )
    ) or not isinstance(result.get("dimension"), int) or result["dimension"] <= 0:
        raise ReleaseQueryError("active report query contract is invalid")
    return result


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".query-{uuid.uuid4().hex[:12]}.tmp"
    try:
        encoded = (canonical_json(payload) + "\n").encode("utf-8")
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        os.chmod(path, stat.S_IREAD)
    finally:
        temporary.unlink(missing_ok=True)


def _catalog(root: Path) -> Path:
    direct = root / "catalog.sqlite3"
    return direct if direct.is_file() else root / "retrieval" / "v2" / "catalog.sqlite3"


def _safe_directory(value: str | Path) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_dir() or path.is_symlink():
        raise ReleaseQueryError("data root is unavailable or unsafe")
    return path


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
