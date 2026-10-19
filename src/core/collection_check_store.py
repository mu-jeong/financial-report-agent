"""Persist report collection checks separately from searchable document counts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.configs import config
from src.core.artifact_io import atomic_write_json


SCHEMA_VERSION = 1
MAX_CHECKS = 200


class CollectionCheckStoreError(RuntimeError):
    """Raised when an existing collection-check store cannot be read safely."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store_path(data_root: str | Path | None = None) -> Path:
    root = Path(data_root or config.DATA_ROOT)
    return root / "data_update" / "collection_checks.json"


def _load_payload(data_root: str | Path | None = None) -> dict[str, Any]:
    path = store_path(data_root)
    try:
        raw_payload = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "checks": []}
    except (OSError, UnicodeError) as exc:
        raise CollectionCheckStoreError("collection check history could not be read") from exc
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise CollectionCheckStoreError("collection check history is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CollectionCheckStoreError("collection check history has an invalid root value")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise CollectionCheckStoreError("collection check history schema is unsupported")
    if not isinstance(payload.get("checks"), list):
        raise CollectionCheckStoreError("collection check history has an invalid checks value")
    return payload


def list_checks(
    data_root: str | Path | None = None,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if int(limit) <= 0:
        return []
    checks = [item for item in _load_payload(data_root)["checks"] if isinstance(item, dict)]
    return [dict(item) for item in reversed(checks[-int(limit):])]


def latest_matching_check(
    *,
    selected_dates: Iterable[str],
    categories: Iterable[str],
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    expected_dates = sorted(set(selected_dates))
    expected_categories = sorted(set(categories))
    for check in list_checks(data_root, limit=MAX_CHECKS):
        if sorted(check.get("selected_dates") or []) != expected_dates:
            continue
        if sorted(check.get("categories") or []) != expected_categories:
            continue
        return check
    return None


def start_check(
    *,
    check_id: str,
    selected_dates: Iterable[str],
    date_ranges: Iterable[tuple[str, str]],
    categories: Iterable[str],
    label: str,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    payload = _load_payload(data_root)
    record = {
        "check_id": check_id,
        "state": "running",
        "attempted_at": _now_iso(),
        "completed_at": None,
        "selected_dates": sorted(set(selected_dates)),
        "date_ranges": [list(value) for value in date_ranges],
        "categories": sorted(set(categories)),
        "label": label,
        "completed_range_count": 0,
        "processed_count": 0,
        "failed_count": 0,
    }
    checks = [item for item in payload["checks"] if isinstance(item, dict)]
    checks.append(record)
    payload = {"schema_version": SCHEMA_VERSION, "checks": checks[-MAX_CHECKS:]}
    atomic_write_json(store_path(data_root), payload)
    return dict(record)


def finish_check(
    check_id: str,
    *,
    state: str,
    completed_range_count: int,
    processed_count: int,
    failed_count: int,
    error: str | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    if state not in {"completed", "empty", "partial", "failed"}:
        raise ValueError(f"unsupported collection check state: {state}")
    payload = _load_payload(data_root)
    checks = [item for item in payload["checks"] if isinstance(item, dict)]
    updated: dict[str, Any] | None = None
    for index in range(len(checks) - 1, -1, -1):
        if checks[index].get("check_id") != check_id:
            continue
        updated = {
            **checks[index],
            "state": state,
            "completed_at": _now_iso(),
            "completed_range_count": max(0, int(completed_range_count)),
            "processed_count": max(0, int(processed_count)),
            "failed_count": max(0, int(failed_count)),
        }
        if error:
            updated["error"] = error
        else:
            updated.pop("error", None)
        checks[index] = updated
        break
    if updated is None:
        return None
    atomic_write_json(
        store_path(data_root),
        {"schema_version": SCHEMA_VERSION, "checks": checks[-MAX_CHECKS:]},
    )
    return dict(updated)
