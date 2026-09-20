"""Bounded research operations independent of MCP and upstream implementation types."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from typing import Any, Callable

from pydantic import ValidationError

from .contracts import INPUT_MODELS, ResultMeta, ToolFailure, ToolResult

LOGGER = logging.getLogger(__name__)


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class ResearchService:
    """One in-flight operation per instance; no unbounded executor queue or hidden retries."""

    def __init__(self, adapter: Any = None, *, adapter_factory: Callable[[], Any] | None = None) -> None:
        if adapter is None and adapter_factory is None:
            raise ValueError("An adapter or adapter factory is required.")
        self.adapter = adapter
        self._adapter_factory = adapter_factory
        self._cursor_key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._closed = False

    def _encode_cursor(self, scope: dict, revision: str, offset: int) -> str:
        payload = _json({"v": 1, "scope": scope, "revision": revision, "offset": offset})
        signature = hmac.digest(self._cursor_key, payload, "sha256")
        return base64.urlsafe_b64encode(signature + payload).decode("ascii")

    def _decode_cursor(self, token: str | None, scope: dict) -> tuple[int, str | None]:
        if token is None:
            return 0, None
        try:
            raw = base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True)
            signature, payload = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, payload, "sha256")):
                raise ValueError("invalid signature")
            value = json.loads(payload)
            if value["v"] != 1 or value["scope"] != scope:
                raise ValueError("cursor does not match request")
            offset, revision = value["offset"], value["revision"]
            if type(offset) is not int or offset < 0 or not isinstance(revision, str) or not revision:
                raise ValueError("invalid cursor payload")
            return offset, revision
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise ToolFailure("INVALID_ARGUMENT", "Cursor is invalid, belongs to another query, or expired after server restart.") from None

    def call(self, name: str, arguments: dict | None = None) -> dict:
        model = INPUT_MODELS.get(name)
        if model is None:
            raise ToolFailure("INVALID_ARGUMENT", "Unknown tool name.")
        try:
            request = model.model_validate({} if arguments is None else arguments)
        except ValidationError as exc:
            # Do not echo arbitrary input values (which may contain credentials).
            fields = sorted({".".join(str(part) for part in error["loc"]) or "arguments" for error in exc.errors()})
            raise ToolFailure("INVALID_ARGUMENT", "Invalid fields: " + ", ".join(fields)) from None
        if not self._lock.acquire(blocking=False):
            raise ToolFailure("BUSY", "Another request is running; retry when it finishes.", True)
        started = time.monotonic()
        try:
            if self._closed:
                raise ToolFailure("NOT_READY", "The research service is closed.")
            if self.adapter is None:
                try:
                    self.adapter = self._adapter_factory()
                except Exception:
                    raise ToolFailure("UPSTREAM_INCOMPATIBLE", "Unable to initialize the local report adapter; check dependencies and configuration.") from None
            filters = request.filters.model_dump(exclude_none=True) if hasattr(request, "filters") else {}
            scope = {"tool": name, "filters": filters}
            if name == "get_report_stats":
                scope["group_by"] = request.group_by
            if name == "read_report":
                scope["report_uid"] = request.report_uid
            # Keep the signed cursor bounded even for large filter arrays.
            cursor_scope = {"request_sha256": hashlib.sha256(_json(scope)).hexdigest()}
            offset, cursor_revision = self._decode_cursor(getattr(request, "cursor", None), cursor_scope)
            if name == "get_status":
                raw = self.adapter.get_status()
            elif name == "list_reports":
                raw = self.adapter.list_reports(filters, request.limit, offset, cursor_revision)
            elif name == "get_report_stats":
                raw = self.adapter.get_report_stats(filters, request.group_by, request.limit, offset, cursor_revision)
            elif name == "search_reports":
                raw = self.adapter.search_reports(request.query, filters, request.top_k)
            else:
                if request.expected_revision and cursor_revision and request.expected_revision != cursor_revision:
                    raise ToolFailure("INVALID_ARGUMENT", "expected_revision does not match the cursor.")
                raw = self.adapter.read_report(request.report_uid, request.expected_revision or cursor_revision, offset, request.max_chars)
            revision = raw.get("revision")
            next_offset = raw.get("next_offset")
            next_cursor = None
            if next_offset is not None:
                if not revision or type(next_offset) is not int or next_offset <= offset:
                    raise RuntimeError("Invalid adapter pagination contract")
                next_cursor = self._encode_cursor(cursor_scope, revision, next_offset)
            _json(raw["data"])  # Pydantic JSON mode would silently turn NaN into null.
            result = ToolResult(
                data=raw["data"],
                meta=ResultMeta(
                    request_id=uuid.uuid4().hex,
                    applied_filters=filters,
                    revision=revision,
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                    warnings=raw.get("warnings", []),
                    truncated=next_cursor is not None,
                    next_cursor=next_cursor,
                ),
            ).model_dump(mode="json")
            _json(result)  # Reject non-JSON/non-finite upstream values before reaching the transport.
            return result
        except ToolFailure:
            raise
        except Exception as exc:
            LOGGER.error("Research operation %s failed (%s)", name, type(exc).__name__)
            raise ToolFailure("INTERNAL_ERROR", "The operation failed; check local compatibility tests and configuration.") from None
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                if self.adapter is not None:
                    self.adapter.close()
