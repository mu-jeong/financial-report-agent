"""Read company report bodies from one consistent, read-only native catalog.

This path does not query FAISS or call an embedding provider. Bodies are copied
inside a SQLite read transaction so later publication cannot change a job's
input. The local inventory is advisory; it cannot prove external completeness.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from src.configs import config
from src.core.report_metadata import parse_filename
from src.retrieval.schema import SCHEMA_VERSION

_SQL_STRIP_CHARS = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)


class ReportCorpusError(RuntimeError):
    """The native catalog cannot safely provide the requested report bodies."""


def validate_scope(company: str, start_date: str, end_date: str) -> tuple[str, str, str]:
    company = str(company or "").strip()
    if not company:
        raise ValueError("회사를 선택해 주세요.")
    try:
        start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    except (TypeError, ValueError) as exc:
        raise ValueError("발행 시작일과 종료일을 YYYY-MM-DD 형식으로 지정해 주세요.") from exc
    if start > end:
        raise ValueError("발행 시작일은 종료일보다 늦을 수 없습니다.")
    if end > date.today():
        raise ValueError("미래 발행일은 선택할 수 없습니다.")
    return company, start.isoformat(), end.isoformat()


@contextmanager
def _catalog(*, data_root=None, catalog_path=None):
    root = Path(data_root or config.DATA_ROOT).expanduser().resolve()
    path = Path(catalog_path) if catalog_path is not None else root / "retrieval/v2/catalog.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ReportCorpusError("검색용 자료가 준비되지 않았습니다. 데이터 상태를 확인해 주세요.")
    try:
        connection = sqlite3.connect(f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ReportCorpusError("검색 자료에 연결할 수 없습니다.") from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        row = connection.execute(
            """SELECT runtime.schema_version, runtime.active_snapshot_id,
                      runtime.active_build_id, runtime.publication_generation,
                      build.profile_id, build.source_manifest_json,
                      build.state AS build_state, snapshot.state AS snapshot_state
                 FROM retrieval_runtime AS runtime
                 LEFT JOIN retrieval_builds AS build ON build.build_id = runtime.active_build_id
                 LEFT JOIN vector_snapshots AS snapshot
                   ON snapshot.snapshot_id = runtime.active_snapshot_id
                  AND snapshot.build_id = build.build_id
                WHERE runtime.runtime_id = 1"""
        ).fetchone()
        if row is None or int(row["schema_version"]) != SCHEMA_VERSION:
            raise ReportCorpusError("지원하는 검색 자료 형식이 아닙니다.")
        if not row["active_snapshot_id"]:
            yield connection, root, None
            return
        if row["build_state"] != "fully_complete" or row["snapshot_state"] != "ready":
            raise ReportCorpusError("검색 자료의 게시 상태를 확인할 수 없습니다.")
        delta = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='retrieval_delta_segments'"
        ).fetchone()
        revision = {
            "snapshot_id": row["active_snapshot_id"],
            "build_id": row["active_build_id"],
            "publication_generation": row["publication_generation"],
            "profile_id": row["profile_id"],
            "delta_generation": 0,
        }
        if delta:
            revision["delta_generation"] = connection.execute(
                """SELECT coalesce(max(sequence), 0) FROM retrieval_delta_segments
                    WHERE base_snapshot_id=? AND base_publication_generation=? AND state='ready'""",
                (row["active_snapshot_id"], row["publication_generation"]),
            ).fetchone()[0]
        yield connection, root, {"revision": revision, "manifest": row["source_manifest_json"], "delta": bool(delta)}
    except sqlite3.Error as exc:
        raise ReportCorpusError("리포트 본문을 읽을 수 없습니다. 검색 자료 상태를 확인해 주세요.") from exc
    finally:
        connection.close()


def list_companies(*, data_root=None, catalog_path=None, pdf_dir=None) -> list[str]:
    with _catalog(data_root=data_root, catalog_path=catalog_path) as (connection, root, state):
        rows = connection.execute(
            """SELECT DISTINCT target_name FROM active_reports
                WHERE report_type='company' AND target_name IS NOT NULL
                ORDER BY target_name"""
        ).fetchall() if state else []
        # Failed-only companies in the active build should also be selectable.
        try:
            excluded_uids = [entry["report_uid"] for entry in json.loads(state["manifest"]).get("reports", [])
                             if entry.get("status") == "excluded"]
        except (TypeError, ValueError, KeyError, AttributeError):
            excluded_uids = []
        names = {row[0] for row in rows if str(row[0]).strip()}
        for uid in excluded_uids:
            failed = connection.execute(
                "SELECT target_name FROM reports WHERE report_uid=? AND report_type='company'", (uid,)
            ).fetchone()
            if failed and failed[0]:
                names.add(failed[0])
        if state and state["delta"]:
            for head in _ready_heads(connection, state["revision"]):
                if head["action"] != "failed":
                    continue
                failed = connection.execute(
                    "SELECT target_name FROM reports WHERE report_uid=? AND report_type='company'", (head["report_uid"],)
                ).fetchone()
                if failed and failed[0]:
                    names.add(failed[0])
        directory = Path(pdf_dir or config.REPORT_PDF_DIR).expanduser()
        for path in directory.glob("*.pdf"):
            try:
                path.resolve().relative_to(root)
            except ValueError:
                continue
            metadata = parse_filename(path.name)
            if metadata and metadata["report_type"] == "company":
                names.add(metadata["target_name"])
        return sorted(names)


def _metadata(row) -> dict:
    value = dict(row)
    value["file_name"] = PurePosixPath(value["canonical_relative_path"]).name
    return value


def _ready_heads(connection, revision) -> list[dict]:
    return [dict(row) for row in connection.execute(
        """WITH ordered AS (
               SELECT action.*, row_number() OVER (
                   PARTITION BY action.canonical_relative_path
                   ORDER BY segment.sequence DESC, segment.segment_id DESC) AS position
               FROM retrieval_delta_reports AS action
               JOIN retrieval_delta_segments AS segment ON segment.segment_id=action.segment_id
               WHERE segment.base_snapshot_id=? AND segment.base_publication_generation=?
                 AND segment.state='ready')
           SELECT * FROM ordered WHERE position=1""",
        (revision["snapshot_id"], revision["publication_generation"]),
    ).fetchall()]


def _known_exclusions(connection, state, company, start, end, active_paths):
    excluded, deleted_paths, warnings = [], set(), []
    try:
        entries = json.loads(state["manifest"]).get("reports", [])
    except (TypeError, ValueError, AttributeError) as exc:
        raise ReportCorpusError("검색 자료의 대상 목록을 읽을 수 없습니다.") from exc
    heads = _ready_heads(connection, state["revision"]) if state["delta"] else []
    head_by_path = {head["canonical_relative_path"]: head for head in heads}
    deleted_paths = {head["canonical_relative_path"] for head in heads if head["action"] == "delete"}
    decisions = [(entry.get("report_uid"), entry.get("reason_code", "본문 준비 실패"))
                 for entry in entries if entry.get("status") == "excluded"]
    decisions.extend((head["report_uid"], head["reason_code"]) for head in heads if head["action"] == "failed")
    seen = set()
    for uid, reason in decisions:
        row = connection.execute("SELECT * FROM reports WHERE report_uid=?", (uid,)).fetchone()
        if row is None:
            continue
        path = row["canonical_relative_path"]
        if path in deleted_paths or path in seen:
            continue
        head = head_by_path.get(path)
        if head and head["action"] == "upsert":
            continue
        if head and head["action"] == "failed" and head["report_uid"] != uid:
            continue  # A newer failure supersedes the base exclusion's metadata.
        if row["report_type"] != "company" or row["target_name"] != company or not start <= row["report_date"] <= end:
            continue
        if path in active_paths:
            warnings.append(f"{PurePosixPath(path).name}: 새 자료의 본문 준비에 실패해 기존 검색용 본문을 사용합니다.")
            continue
        seen.add(path)
        excluded.append({**_metadata(row), "reason": f"본문 준비 실패 ({reason})"})
    return excluded, deleted_paths, warnings


def _inventory(root, company, start, end, documents, excluded, deleted_paths, pdf_dir=None):
    directory = Path(pdf_dir or config.REPORT_PDF_DIR).expanduser()
    if not directory.is_dir():
        return []
    active_by_path = {doc["canonical_relative_path"]: doc for doc in documents}
    known_paths = set(active_by_path) | {doc["canonical_relative_path"] for doc in excluded} | deleted_paths
    warnings = []
    for path in sorted(directory.glob("*.pdf")):
        metadata = parse_filename(path.name)
        if not metadata or metadata["report_type"] != "company" or metadata["target_name"] != company:
            continue
        if not start <= metadata["report_date"] <= end:
            continue
        try:
            canonical = path.resolve().relative_to(root).as_posix()
        except ValueError:
            # Inventory outside DATA_ROOT cannot be reliably paired to native identities.
            continue
        if canonical in deleted_paths:
            continue
        if canonical in active_by_path:
            try:
                current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if current_hash != active_by_path[canonical]["source_sha256"]:
                    warnings.append(f"{path.name}: 원문 파일이 변경되어 기존 검색용 본문을 사용합니다. 데이터 업데이트 후 다시 생성해 주세요.")
            except OSError:
                warnings.append(f"{path.name}: 현재 원문 파일을 읽을 수 없습니다. 저장된 본문을 사용합니다.")
        elif canonical not in known_paths:
            excluded.append({**metadata, "canonical_relative_path": canonical, "file_name": path.name,
                             "reason": "다운로드된 자료의 본문이 아직 준비되지 않았습니다."})
            known_paths.add(canonical)
    return warnings


def _validate_workload_preflight(connection, profile_id, company, start, end):
    # Local import keeps corpus loading out of report_synthesis's import graph.
    from src.core.report_synthesis import (
        CHUNK_CHARS,
        MAX_BODY_CHARS,
        MAX_CHUNKS,
        MAX_DOCUMENTS,
    )

    workload = connection.execute(
        """WITH scoped_reports AS (
               SELECT report_id
               FROM active_reports
               WHERE report_type='company' AND target_name=?
                 AND report_date>=? AND report_date<=?
           ), report_workload AS (
               SELECT report.report_id,
                      sum(length(parent.content)) AS body_chars,
                      sum((length(parent.content) + ? - 1) / ?) AS chunks
               FROM scoped_reports AS report
               JOIN retrieval_parents AS parent
                 ON parent.report_id=report.report_id AND parent.profile_id=?
               GROUP BY report.report_id
               HAVING max(length(trim(parent.content, ?)) > 0)
           )
           SELECT count(*) AS documents,
                  coalesce(sum(body_chars), 0) AS body_chars,
                  coalesce(sum(chunks), 0) AS chunks
           FROM report_workload""",
        (company, start, end, CHUNK_CHARS, CHUNK_CHARS, profile_id, _SQL_STRIP_CHARS),
    ).fetchone()
    document_count = int(workload["documents"])
    body_chars = int(workload["body_chars"])
    chunks = int(workload["chunks"])
    if document_count > MAX_DOCUMENTS or body_chars > MAX_BODY_CHARS or chunks > MAX_CHUNKS:
        raise ValueError(
            f"분석 범위가 실행 한도를 초과했습니다. 기간을 줄여 주세요 "
            f"(최대 {MAX_DOCUMENTS}건, 본문 {MAX_BODY_CHARS:,}자, {MAX_CHUNKS}구간)."
        )


def load_report_corpus(company: str, start_date: str, end_date: str, *, data_root=None, catalog_path=None, pdf_dir=None) -> dict:
    company, start, end = validate_scope(company, start_date, end_date)
    with _catalog(data_root=data_root, catalog_path=catalog_path) as (connection, root, state):
        documents, excluded, warnings = [], [], []
        available_period = None
        revision = state["revision"] if state else {}
        if state:
            available = connection.execute(
                """SELECT min(report_date), max(report_date), count(*) FROM active_reports
                    WHERE report_type='company' AND target_name=?""", (company,),
            ).fetchone()
            if available[2]:
                available_period = {"start_date": available[0], "end_date": available[1],
                                    "documents": available[2]}
            _validate_workload_preflight(connection, revision["profile_id"], company, start, end)
            rows = connection.execute(
                """SELECT * FROM active_reports WHERE report_type='company' AND target_name=?
                    AND report_date>=? AND report_date<=?
                    ORDER BY report_date, broker, title, report_uid""", (company, start, end),
            ).fetchall()
            for row in rows:
                doc = _metadata(row)
                parents = connection.execute(
                    """SELECT parent_uid, parent_order, content, content_sha256
                       FROM retrieval_parents WHERE report_id=? AND profile_id=?
                       ORDER BY parent_order, parent_uid""", (row["report_id"], revision["profile_id"]),
                ).fetchall()
                doc["parents"] = [dict(parent) for parent in parents]
                if not parents or not any(parent["content"].strip() for parent in parents):
                    excluded.append({**doc, "reason": "분석할 저장 본문이 없습니다."})
                else:
                    documents.append(doc)
            active_paths = {row["canonical_relative_path"] for row in rows}
            known_excluded, deleted, known_warnings = _known_exclusions(connection, state, company, start, end, active_paths)
            excluded.extend(known_excluded)
            warnings.extend(known_warnings)
        else:
            deleted = set()
        # No provider call or long-running operation occurs while the read transaction is open.
    warnings.extend(_inventory(root, company, start, end, documents, excluded, deleted, pdf_dir))
    corpus = {"company": company, "start_date": start, "end_date": end, "revision": revision,
              "profile_id": revision.get("profile_id"), "documents": documents, "excluded": excluded,
              "warnings": list(dict.fromkeys(warnings)), "available_period": available_period}
    # Scope identity excludes unrelated publication generations but includes all document versions.
    fingerprint = {"company": company, "start_date": start, "end_date": end,
                   "profile_id": corpus["profile_id"],
                   "documents": [{"report_uid": doc["report_uid"],
                                  "parents": [(parent["parent_uid"], parent["content_sha256"]) for parent in doc["parents"]]}
                                 for doc in documents],
                   "excluded": [(doc.get("report_uid"), doc["canonical_relative_path"], doc["reason"]) for doc in excluded],
                   "warnings": corpus["warnings"]}
    corpus["fingerprint"] = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return corpus
