from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp_types import CallToolRequestParams, PaginatedRequestParams

from integrations.finance_mcp.contracts import INPUT_MODELS, ToolFailure, ToolResult
from integrations.finance_mcp.server import create_server
from integrations.finance_mcp.tests.upstream_fixture import create_native_fixture, digest


REPO_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO_ROOT / "integrations" / "finance_mcp" / "__main__.py"
SDK_FIXTURE_LAUNCHER = """
import sys
from pathlib import Path

from integrations.finance_mcp.server import create_server, run_stdio
from integrations.finance_mcp.service import ResearchService
from integrations.finance_mcp.tests.upstream_fixture import FakeEmbeddings
from integrations.finance_mcp.upstream_adapter import UpstreamAdapter

adapter = UpstreamAdapter(Path(sys.argv[1]))
adapter._embeddings = FakeEmbeddings()
run_stdio(create_server(ResearchService(adapter)))
"""


def _result(name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "data": {"tool": name},
        "meta": {
            "request_id": "request-1",
            "applied_filters": {},
            "revision": "revision-1",
            "elapsed_ms": 1.0,
            "warnings": [],
            "truncated": False,
            "next_cursor": None,
        },
    }


class FakeService:
    def __init__(self) -> None:
        self.closed = False

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "search_reports" and arguments.get("query") == "fail":
            raise ToolFailure("PROVIDER_UNAVAILABLE", "provider unavailable", True)
        if name not in INPUT_MODELS:
            raise ToolFailure("INVALID_ARGUMENT", "Unknown tool name.")
        INPUT_MODELS[name].model_validate(arguments)
        return _result(name)

    def close(self) -> None:
        self.closed = True


def _handler(server: Any, method: str):
    entry = server.get_request_handler(method)
    assert entry is not None
    return entry.handler


def test_public_handlers_publish_exact_contracts_and_structured_results() -> None:
    service = FakeService()
    server = create_server(service)

    async def exercise() -> None:
        listed = await _handler(server, "tools/list")(None, PaginatedRequestParams())
        assert [tool.name for tool in listed.tools] == list(INPUT_MODELS)
        for tool in listed.tools:
            assert tool.input_schema == INPUT_MODELS[tool.name].model_json_schema(mode="validation")
            assert tool.output_schema == ToolResult.model_json_schema(mode="serialization")
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False

        called = await _handler(server, "tools/call")(
            None,
            CallToolRequestParams(name="get_status", arguments={}),
        )
        assert called.is_error is not True
        assert called.structured_content == _result("get_status")

        failed = await _handler(server, "tools/call")(
            None,
            CallToolRequestParams(name="search_reports", arguments={"query": "fail"}),
        )
        assert failed.is_error is True
        assert json.loads(failed.content[0].text) == {
            "code": "PROVIDER_UNAVAILABLE",
            "message": "provider unavailable",
            "retryable": True,
        }

        async with server.lifespan(server):
            pass

    asyncio.run(exercise())
    assert service.closed is True


def _read_response(process: subprocess.Popen[str]) -> tuple[dict[str, Any], str]:
    received: queue.Queue[str] = queue.Queue(maxsize=1)
    threading.Thread(target=lambda: received.put(process.stdout.readline()), daemon=True).start()  # type: ignore[union-attr]
    try:
        line = received.get(timeout=30)
    except queue.Empty:
        process.kill()
        raise AssertionError("timed out waiting for an MCP response") from None
    assert line, "MCP server closed stdout before replying"
    return json.loads(line), line


def _run_stdio(
    *,
    repo_root: Path,
    data_root: Path | None,
    cwd: Path,
    calls: list[tuple[int, str, dict[str, Any]]],
    command_override: list[str] | None = None,
) -> tuple[dict[int, dict[str, Any]], str, str]:
    command = command_override or [sys.executable, str(ENTRYPOINT), "--repo-root", str(repo_root)]
    if command_override is None and data_root is not None:
        command.extend(("--data-root", str(data_root)))
    environment = os.environ.copy()
    environment["OPENROUTER_API_KEY"] = ""
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    process = subprocess.Popen(
        command,
        text=True,
        encoding="utf-8",
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=environment,
    )
    assert process.stdin is not None
    responses: dict[int, dict[str, Any]] = {}
    stdout_lines: list[str] = []

    def request(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
        response, line = _read_response(process)
        responses[response["id"]] = response
        stdout_lines.append(line)

    request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "finance-mcp-test", "version": "1"},
            },
        }
    )
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
    process.stdin.flush()
    request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    for request_id, name, arguments in calls:
        request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    process.stdin.close()
    return_code = process.wait(timeout=30)
    remaining_stdout = process.stdout.read() if process.stdout is not None else ""
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert return_code == 0, stderr
    assert remaining_stdout == "", f"stdout contained non-protocol text: {remaining_stdout!r}"
    stdout = "".join(stdout_lines)
    return responses, stdout, stderr


def test_stdio_list_is_lazy_and_clean_from_arbitrary_unicode_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "임의 작업 폴더"
    repo_root = tmp_path / "repo root without src"
    cwd.mkdir()
    repo_root.mkdir()

    responses, stdout, _stderr = _run_stdio(
        repo_root=repo_root,
        data_root=None,
        cwd=cwd,
        calls=[],
    )

    assert set(responses) == {1, 2}
    assert responses[1]["result"]["serverInfo"]["name"] == "finance-research"
    assert {tool["name"] for tool in responses[2]["result"]["tools"]} == set(INPUT_MODELS)
    assert stdout.count("\n") == 2


def test_stdio_calls_all_tools_reports_errors_and_shuts_down(tmp_path: Path) -> None:
    fixture = create_native_fixture(tmp_path / "fixture with spaces")
    report_uid = digest("report-1")
    cwd = tmp_path / "호출 cwd"
    cwd.mkdir()
    calls = [
        (3, "get_status", {}),
        (4, "list_reports", {"filters": {"target_names": ["Alpha"]}, "limit": 10}),
        (5, "get_report_stats", {"group_by": "report_type"}),
        (6, "read_report", {"report_uid": report_uid, "max_chars": 10}),
        (7, "search_reports", {"query": "alpha"}),
        (8, "list_reports", {"unexpected": "do-not-echo-this-secret"}),
    ]

    responses, _stdout, _stderr = _run_stdio(
        repo_root=REPO_ROOT,
        data_root=fixture.data_root,
        cwd=cwd,
        calls=calls,
    )

    assert set(responses) == set(range(1, 9))
    for request_id in (3, 4, 5, 6):
        result = responses[request_id]["result"]
        assert result.get("isError") is not True
        assert result["structuredContent"]["schema_version"] == 1
    assert responses[4]["result"]["structuredContent"]["data"]["reports"]
    assert responses[6]["result"]["structuredContent"]["data"]["content"] == "alpha firs"

    search_error = responses[7]["result"]
    assert search_error["isError"] is True
    assert json.loads(search_error["content"][0]["text"])["code"] == "PROVIDER_UNAVAILABLE"

    validation_error = responses[8]["result"]
    assert validation_error["isError"] is True
    error_payload = json.loads(validation_error["content"][0]["text"])
    assert error_payload["code"] == "INVALID_ARGUMENT"
    assert "do-not-echo-this-secret" not in validation_error["content"][0]["text"]


def test_stdio_diverts_service_stdout_away_from_protocol(tmp_path: Path) -> None:
    script = """
from integrations.finance_mcp.server import create_server, run_stdio

class NoisyService:
    def call(self, name, arguments):
        print("third-party-call-noise", flush=True)
        return {
            "schema_version": 1,
            "data": {"tool": name},
            "meta": {
                "request_id": "noise-test",
                "applied_filters": {},
                "revision": None,
                "elapsed_ms": 0.0,
                "warnings": [],
                "truncated": False,
                "next_cursor": None,
            },
        }

    def close(self):
        print("third-party-close-noise", flush=True)

run_stdio(create_server(NoisyService()))
"""
    responses, stdout, stderr = _run_stdio(
        repo_root=REPO_ROOT,
        data_root=None,
        cwd=tmp_path,
        calls=[(3, "get_status", {})],
        command_override=[sys.executable, "-c", script],
    )

    assert responses[3]["result"]["structuredContent"]["data"] == {"tool": "get_status"}
    assert "third-party" not in stdout
    assert "third-party-call-noise" in stderr
    assert "third-party-close-noise" in stderr


def test_official_sdk_searches_and_pages_read_without_network(tmp_path: Path) -> None:
    fixture = create_native_fixture(tmp_path / "sdk fixture")
    cwd = tmp_path / "SDK 클라이언트 cwd"
    cwd.mkdir()
    environment = os.environ.copy()
    environment["OPENROUTER_API_KEY"] = ""
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", SDK_FIXTURE_LAUNCHER, str(fixture.data_root)],
        env=environment,
        cwd=cwd,
    )

    async def exercise() -> None:
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {tool.name for tool in listed.tools} == set(INPUT_MODELS)

                searched = await session.call_tool(
                    "search_reports",
                    {"query": "alpha outlook", "top_k": 2},
                )
                assert searched.is_error is not True
                assert searched.structured_content is not None
                search_payload = ToolResult.model_validate(searched.structured_content)
                first_hit = search_payload.data["results"][0]
                assert first_hit["report_uid"] == digest("report-1")

                first_page = await session.call_tool(
                    "read_report",
                    {
                        "report_uid": first_hit["report_uid"],
                        "expected_revision": search_payload.meta.revision,
                        "max_chars": 5,
                    },
                )
                assert first_page.is_error is not True
                page_one = ToolResult.model_validate(first_page.structured_content)
                assert page_one.data["content"] == "alpha"
                assert page_one.meta.truncated is True
                assert page_one.meta.next_cursor is not None

                second_page = await session.call_tool(
                    "read_report",
                    {
                        "report_uid": first_hit["report_uid"],
                        "expected_revision": search_payload.meta.revision,
                        "cursor": page_one.meta.next_cursor,
                        "max_chars": 30_000,
                    },
                )
                assert second_page.is_error is not True
                page_two = ToolResult.model_validate(second_page.structured_content)
                assert page_one.data["content"] + page_two.data["content"] == "alpha first indexed body"
                assert page_two.meta.next_cursor is None

    anyio.run(exercise)


def test_official_sdk_clients_isolate_cursors_across_server_processes(tmp_path: Path) -> None:
    fixture = create_native_fixture(tmp_path / "shared sdk fixture")
    cwd = tmp_path / "동시 SDK cwd"
    cwd.mkdir()
    environment = os.environ.copy()
    environment["OPENROUTER_API_KEY"] = ""
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + environment.get("PYTHONPATH", "")

    def parameters() -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-c", SDK_FIXTURE_LAUNCHER, str(fixture.data_root)],
            env=environment,
            cwd=cwd,
        )

    async def exercise() -> None:
        async with stdio_client(parameters()) as streams_one:
            async with stdio_client(parameters()) as streams_two:
                async with ClientSession(*streams_one) as client_one:
                    async with ClientSession(*streams_two) as client_two:
                        await client_one.initialize()
                        await client_two.initialize()
                        tools_one = await client_one.list_tools()
                        tools_two = await client_two.list_tools()
                        assert {tool.name for tool in tools_one.tools} == set(INPUT_MODELS)
                        assert {tool.name for tool in tools_two.tools} == set(INPUT_MODELS)

                        listed_one = await client_one.call_tool(
                            "list_reports",
                            {"filters": {"target_names": ["Alpha"]}, "limit": 1},
                        )
                        listed_two = await client_two.call_tool(
                            "list_reports",
                            {"filters": {"target_names": ["Alpha"]}, "limit": 1},
                        )
                        list_one = ToolResult.model_validate(listed_one.structured_content)
                        list_two = ToolResult.model_validate(listed_two.structured_content)
                        report_one = list_one.data["reports"][0]
                        report_two = list_two.data["reports"][0]

                        first_page = await client_one.call_tool(
                            "read_report",
                            {
                                "report_uid": report_one["report_uid"],
                                "expected_revision": list_one.meta.revision,
                                "max_chars": 5,
                            },
                        )
                        page_one = ToolResult.model_validate(first_page.structured_content)
                        assert page_one.meta.next_cursor is not None

                        independent_read = await client_two.call_tool(
                            "read_report",
                            {
                                "report_uid": report_two["report_uid"],
                                "expected_revision": list_two.meta.revision,
                                "max_chars": 30_000,
                            },
                        )
                        assert independent_read.is_error is not True
                        assert ToolResult.model_validate(independent_read.structured_content).data["content"]

                        foreign_cursor = await client_two.call_tool(
                            "read_report",
                            {
                                "report_uid": report_one["report_uid"],
                                "expected_revision": list_one.meta.revision,
                                "cursor": page_one.meta.next_cursor,
                                "max_chars": 30_000,
                            },
                        )
                        assert foreign_cursor.is_error is True
                        assert json.loads(foreign_cursor.content[0].text)["code"] == "INVALID_ARGUMENT"

                        continued = await client_one.call_tool(
                            "read_report",
                            {
                                "report_uid": report_one["report_uid"],
                                "expected_revision": list_one.meta.revision,
                                "cursor": page_one.meta.next_cursor,
                                "max_chars": 30_000,
                            },
                        )
                        assert continued.is_error is not True
                        page_two = ToolResult.model_validate(continued.structured_content)
                        combined = page_one.data["content"] + page_two.data["content"]
                        assert len(combined) == page_one.data["total_chars"]
                        assert page_two.meta.next_cursor is None

    anyio.run(exercise)
