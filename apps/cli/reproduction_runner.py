"""Generic historical runner for registered Finance LLM app bytes.

This harness lives in a release bundle's runtime directory. It constructs an
ephemeral Native V2 layout from exactly one FixedSnapshot and imports the
registered ``app/`` tree, never the current checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Mapping


_PROFILE_ENVIRONMENT_KEYS = {
    "CHILD_CHUNK_SIZE",
    "CHUNK_SIZE",
    "EMBEDDING_MODEL",
    "GENERATION_MODEL",
    "OPENROUTER_APP_TITLE",
    "OPENROUTER_APP_URL",
    "OPENROUTER_DATA_COLLECTION",
    "PARENT_CHUNK_SIZE",
    "RECENCY_WEIGHT",
    "RERANK_MODEL",
    "RERANK_PROVIDER",
    "RERANK_TIMEOUT",
    "SEARCH_CANDIDATE_MULTIPLIER",
    "SEARCH_TOP_K",
    "USE_PARENT_CHILD",
    "USE_RERANKER",
    "VECTOR_RETRIEVAL_CONCURRENCY",
}


def _required_environment(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing runner environment: {name}")
    return Path(value).expanduser().resolve(strict=True)


def _copy_exact(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"FixedSnapshot file is unavailable: {source.name}")
    if target.exists() or target.is_symlink():
        if (
            target.is_symlink()
            or not target.is_file()
            or _file_identity(target) != _file_identity(source)
        ):
            raise RuntimeError(f"isolated runtime file conflicts: {target.name}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, target.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    return {"sha256": _sha256_file(path), "size_bytes": path.stat().st_size}


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def _write_exact_once(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise RuntimeError(f"isolated runtime evidence conflicts: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _projected_runtime(
    catalog: Path,
) -> tuple[int, int, str | None, str | None]:
    try:
        connection = sqlite3.connect(
            f"{catalog.resolve(strict=True).as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            rows = connection.execute(
                """
                SELECT publication_generation, write_epoch,
                       active_snapshot_id, active_build_id
                FROM retrieval_runtime WHERE runtime_id=1
                """
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError("FixedSnapshot projected catalog is invalid") from exc
    if len(rows) != 1:
        raise RuntimeError("FixedSnapshot projected runtime singleton is invalid")
    generation, write_epoch, active_snapshot_id, active_build_id = rows[0]
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(write_epoch, int)
        or isinstance(write_epoch, bool)
        or write_epoch < 0
        or (
            active_snapshot_id is not None
            and (not isinstance(active_snapshot_id, str) or not active_snapshot_id)
        )
        or (
            active_build_id is not None
            and (not isinstance(active_build_id, str) or not active_build_id)
        )
    ):
        raise RuntimeError("FixedSnapshot projected runtime values are invalid")
    return generation, write_epoch, active_snapshot_id, active_build_id


def _prepare_projected_runtime_floor(
    data_root: Path,
    manifest: Mapping[str, Any],
) -> None:
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    files = manifest.get("files")
    expected_identity = (
        files.get("projected_catalog.sqlite3")
        if isinstance(files, Mapping)
        else None
    )
    if expected_identity != _file_identity(catalog):
        raise RuntimeError(
            "FixedSnapshot projected catalog bytes do not match manifest"
        )
    projected_snapshot_id = manifest.get("projected_snapshot_id")
    if not isinstance(projected_snapshot_id, str) or not projected_snapshot_id:
        raise RuntimeError("FixedSnapshot projected snapshot identity is invalid")
    projected_build_id = manifest.get("projected_build_id")
    if not isinstance(projected_build_id, str) or not projected_build_id:
        raise RuntimeError("FixedSnapshot projected build identity is invalid")

    generation, write_epoch, active_snapshot_id, active_build_id = (
        _projected_runtime(catalog)
    )
    if (
        active_snapshot_id != projected_snapshot_id
        or active_build_id != projected_build_id
    ):
        raise RuntimeError("FixedSnapshot projected runtime identity is invalid")
    if generation == 0:
        return
    if generation != 1 or write_epoch != 0:
        raise RuntimeError("FixedSnapshot projected runtime floor is unsupported")

    publication_id = "fixed-snapshot-projection"
    checkpoint_relative_path = (
        "retrieval/v2/backups/"
        f"catalog-current-g{generation}-{publication_id}.sqlite3"
    )
    checkpoint = data_root / Path(checkpoint_relative_path)
    _copy_exact(catalog, checkpoint)
    checkpoint_sha256 = _sha256_file(checkpoint)
    floor = {
        "schema_version": 2,
        "publication_id": publication_id,
        "publication_generation": generation,
        "write_epoch": write_epoch,
        "active_snapshot_id": active_snapshot_id,
        "checkpoint_relative_path": checkpoint_relative_path,
        "checkpoint_sha256": checkpoint_sha256,
    }
    _write_exact_once(
        data_root
        / "retrieval"
        / "v2"
        / "evidence"
        / publication_id
        / "committed-floor.json",
        _canonical_json(floor),
    )


def prepare_isolated_data_root(snapshot_root: Path, workspace: Path) -> Path:
    """Copy only the registered FixedSnapshot bytes into runtime layout."""

    catalog = snapshot_root / "projected_catalog.sqlite3"
    index = snapshot_root / "subset.faiss"
    manifest = snapshot_root / "manifest.json"
    for path in (catalog, index, manifest):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"FixedSnapshot is incomplete: {path.name}")
    try:
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("FixedSnapshot manifest is invalid") from exc
    if not isinstance(manifest_payload, dict):
        raise RuntimeError("FixedSnapshot manifest must be an object")
    files = manifest_payload.get("files")
    expected_catalog = files.get(catalog.name) if isinstance(files, Mapping) else None
    if expected_catalog != _file_identity(catalog):
        raise RuntimeError(
            "FixedSnapshot projected catalog bytes do not match manifest"
        )
    projected_snapshot_id = manifest_payload.get("projected_snapshot_id")
    if not isinstance(projected_snapshot_id, str) or not projected_snapshot_id:
        raise RuntimeError("FixedSnapshot projected snapshot identity is invalid")
    projected_build_id = manifest_payload.get("projected_build_id")
    if not isinstance(projected_build_id, str) or not projected_build_id:
        raise RuntimeError("FixedSnapshot projected build identity is invalid")
    data_root = workspace / "isolated-data"
    _copy_exact(catalog, data_root / "retrieval" / "v2" / "catalog.sqlite3")
    _copy_exact(index, data_root / "subset.faiss")
    _copy_exact(manifest, data_root / "fixed-snapshot-manifest.json")
    _prepare_projected_runtime_floor(data_root, manifest_payload)
    return data_root


def _safe_evidence_ref(source: Mapping[str, Any], position: int) -> dict[str, Any] | None:
    source_uid = source.get("source_uid") or source.get("report_uid")
    source_sha256 = source.get("source_sha256")
    if not isinstance(source_uid, str) or not source_uid:
        return None
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_sha256)
    ):
        return None
    result: dict[str, Any] = {
        "role": "CONTEXT_USED",
        "source_uid": source_uid,
        "source_sha256": source_sha256,
        "rank": int(source.get("rank") or position),
    }
    chunk_uid = source.get("chunk_uid")
    if isinstance(chunk_uid, str) and chunk_uid:
        result["chunk_uid"] = chunk_uid
    locator = source.get("locator")
    if isinstance(locator, str) and locator and len(locator.encode("utf-8")) <= 256:
        result["locator"] = locator
    return result


def build_run_artifact(
    final_state: Mapping[str, Any],
    *,
    latency_ms: float,
    runtime_profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a graph result to the immutable Run evidence contract."""

    answer = final_state.get("generation")
    if not isinstance(answer, str):
        answer = ""
    route = final_state.get("route")
    sources = (
        final_state.get("rerank_info")
        if route == "vectordb"
        else final_state.get("rdb_sources")
    )
    if not isinstance(sources, list):
        sources = []
    evidence_refs = [
        evidence
        for position, source in enumerate(sources, 1)
        if isinstance(source, Mapping)
        and (evidence := _safe_evidence_ref(source, position)) is not None
    ]
    citation_ranks = final_state.get("citation_ranks_used")
    cited = set(citation_ranks) if isinstance(citation_ranks, list) else set()
    for evidence in evidence_refs:
        if evidence.get("rank") in cited:
            evidence["role"] = "CITED"
    return {
        "schema_version": 1,
        "runner_status": "SUCCEEDED",
        "raw_answer": answer,
        "evidence_refs": evidence_refs,
        "route_summary": {
            "route": route,
            "rewritten_query": final_state.get("rewritten_query"),
            "no_vector_results": bool(final_state.get("no_vector_results")),
        },
        "runtime_profile": dict(runtime_profile),
        "latency_ms": round(float(latency_ms), 3),
    }


def apply_runtime_profile_environment(profile: Mapping[str, Any]) -> None:
    environment = profile.get("environment", {})
    if not isinstance(environment, Mapping):
        raise RuntimeError("runtime profile environment must be an object")
    rendered: dict[str, str] = {}
    for key, value in environment.items():
        normalized_key = str(key).upper()
        if normalized_key not in _PROFILE_ENVIRONMENT_KEYS:
            raise RuntimeError(
                f"runtime profile environment key is not allowed: {key}"
            )
        if value is None or isinstance(value, (dict, list)) or "\x00" in str(value):
            raise RuntimeError(
                f"runtime profile environment value is invalid: {key}"
            )
        if isinstance(value, bool):
            rendered[normalized_key] = "true" if value else "false"
        else:
            rendered[normalized_key] = str(value)
    os.environ.update(rendered)


def _write_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    bundle_root = _required_environment("FINANCE_LLM_RELEASE_BUNDLE_ROOT")
    app_root = _required_environment("FINANCE_LLM_RELEASE_APP_ROOT")
    snapshot_root = _required_environment("FINANCE_LLM_FIXED_SNAPSHOT_ROOT")
    workspace = _required_environment("FINANCE_LLM_RUN_WORKSPACE")
    input_path = _required_environment("FINANCE_LLM_RUN_INPUT_PATH")
    artifact_value = os.environ.get("FINANCE_LLM_RUN_ARTIFACT_PATH", "").strip()
    if not artifact_value:
        raise RuntimeError("missing runner artifact path")
    artifact_path = Path(artifact_value).expanduser().resolve()
    try:
        artifact_path.relative_to(workspace)
    except ValueError as exc:
        raise RuntimeError("runner artifact escaped workspace") from exc

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("question"), str):
        raise RuntimeError("runner input requires a question")
    runtime_profile = json.loads(
        (bundle_root / "runtime-profile.json").read_text(encoding="utf-8")
    )
    if not isinstance(runtime_profile, dict):
        raise RuntimeError("runtime profile must be an object")
    apply_runtime_profile_environment(runtime_profile)

    data_root = prepare_isolated_data_root(snapshot_root, workspace)
    os.environ.update(
        {
            "DATA_ROOT": str(data_root),
            "CONVERSATION_DB_PATH": str(workspace / "conversation.sqlite3"),
            "RERANK_CACHE_DIR": str(workspace / "rerank-cache"),
            "ISSUE_REPORT_REMOTE_ENABLED": "false",
            "MONITORING_MODE": "false",
            "CRAWLER_TARGET_DATE": str(
                payload.get("fixed_clock") or "2000-01-01"
            )[:10],
        }
    )
    def belongs_to_another_checkout(entry: str) -> bool:
        try:
            candidate = Path(entry or ".").resolve()
        except OSError:
            return True
        if candidate == app_root or candidate == Path.cwd():
            return candidate != app_root
        # A Finance LLM checkout has both top-level trees.  Excluding all such
        # roots prevents a missing historical module from falling through to
        # the operator's current checkout while retaining stdlib/site-packages.
        return (candidate / "apps").is_dir() and (candidate / "src").is_dir()

    sys.path[:] = [str(app_root)] + [
        entry for entry in sys.path if not belongs_to_another_checkout(entry)
    ]

    started = time.perf_counter()
    try:
        from apps.cli.app import run_search

        final_state = run_search(
            payload["question"],
            thread_id=f"reproduction-{hashlib.sha256(input_path.read_bytes()).hexdigest()[:16]}",
        )
        if not isinstance(final_state, Mapping):
            raise RuntimeError("registered app returned a non-object graph result")
        artifact = build_run_artifact(
            final_state,
            latency_ms=(time.perf_counter() - started) * 1000,
            runtime_profile=runtime_profile,
        )
        _write_artifact(artifact_path, artifact)
        return 0
    except BaseException as exc:
        failure = {
            "schema_version": 1,
            "runner_status": "FAILED",
            "error_type": type(exc).__name__,
            # Exception text can contain credentials, query text, or local
            # paths.  The typed error is enough for the durable artifact;
            # interactive diagnostics stay in the isolated process output.
            "error_message": "registered release execution failed",
            "runtime_profile": runtime_profile,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        _write_artifact(artifact_path, failure)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
