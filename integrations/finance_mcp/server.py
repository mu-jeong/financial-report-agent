"""Public low-level MCP transport for the local finance research service."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from .contracts import INPUT_MODELS, TOOL_DESCRIPTIONS, ToolFailure, ToolResult

LOGGER = logging.getLogger(__name__)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _error_result(failure: ToolFailure) -> CallToolResult:
    payload = {
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=_json(payload))],
        is_error=True,
    )


def _success_result(value: dict[str, Any]) -> CallToolResult:
    result = ToolResult.model_validate(value).model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=_json(result))],
        structured_content=result,
    )


def create_server(service: Any) -> Server[None]:
    """Create the five-tool server without touching upstream data or providers."""

    closed_world = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    provider_read = ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    )
    output_schema = ToolResult.model_json_schema(mode="serialization")
    tools = tuple(
        Tool(
            name=name,
            description=TOOL_DESCRIPTIONS[name],
            input_schema=model.model_json_schema(mode="validation"),
            output_schema=output_schema,
            annotations=provider_read if name == "search_reports" else closed_world,
        )
        for name, model in INPUT_MODELS.items()
    )

    async def list_tools(_ctx: Any, _params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=list(tools))

    async def call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        try:
            value = await anyio.to_thread.run_sync(service.call, params.name, params.arguments or {})
            return _success_result(value)
        except ToolFailure as exc:
            return _error_result(exc)
        except Exception:
            # service.call normally maps implementation failures itself. Keep this
            # final boundary generic so upstream exception text cannot reach a model.
            LOGGER.exception("Unexpected failure while dispatching tool %s", params.name)
            return _error_result(
                ToolFailure(
                    "INTERNAL_ERROR",
                    "The operation failed; check local compatibility tests and configuration.",
                )
            )

    @asynccontextmanager
    async def lifespan(_server: Server[None]):
        try:
            yield None
        finally:
            service.close()

    return Server(
        "finance-research",
        title="Local Finance Report Research",
        description="Read-only access to the repository's local indexed report corpus.",
        version="1.0.0",
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def run_stdio(server: Server[None]) -> None:
    """Run one server connection until the client closes stdin."""

    async def serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    anyio.run(serve)


__all__ = ["create_server", "run_stdio"]
