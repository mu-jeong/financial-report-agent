"""Opt-in end-to-end test against the real corpus and OpenRouter (two searches)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client


async def exercise(root: Path, evidence: dict) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(root / "integrations/finance_mcp/__main__.py"), "--repo-root", str(root)],
        env={**os.environ, "PYTHONUTF8": "1"},
        cwd=root,
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            initialized = await client.initialize()
            evidence["server"] = initialized.server_info.model_dump(mode="json")
            tools = await client.list_tools()
            evidence["tools"] = [tool.name for tool in tools.tools]
            assert set(evidence["tools"]) == {
                "get_status", "list_reports", "get_report_stats", "search_reports", "read_report"
            }, "Expected five tools"

            async def call(name: str, arguments: dict, expected_error: str | None = None):
                started = time.monotonic()
                result = await client.call_tool(name, arguments, read_timeout_seconds=120)
                step = {"tool": name, "seconds": round(time.monotonic() - started, 3)}
                evidence["calls"].append(step)
                if result.is_error:
                    error = json.loads(result.content[0].text)
                    step["error_code"] = error["code"]
                    assert error["code"] == expected_error, f"{name} failed: {error['code']}"
                    step["passed"] = True
                    print(json.dumps(step), flush=True)
                    return None
                assert expected_error is None, f"{name} did not reject invalid input"
                assert result.structured_content, f"{name} missing structured output"
                step["passed"] = True
                print(json.dumps(step), flush=True)
                return result.structured_content

            status = await call("get_status", {})
            assert status["data"]["state"] == "ready", "Corpus is not ready"
            assert status["data"]["capabilities"]["search_reports"], "OpenRouter/model configuration unavailable"
            evidence["corpus"] = status["data"]
            revision = status["meta"]["revision"]

            stats = await call("get_report_stats", {"group_by": "report_type"})
            assert stats["data"]["total_count"] == status["data"]["report_count"]
            assert sum(group["report_count"] for group in stats["data"]["groups"]) == stats["data"]["total_count"]
            evidence["report_groups"] = stats["data"]["groups"]

            first = await call("list_reports", {"limit": 2})
            assert first["meta"]["next_cursor"], "Need more than two indexed reports for this smoke test"
            second = await call("list_reports", {"limit": 2, "cursor": first["meta"]["next_cursor"]})
            first_ids = {report["report_uid"] for report in first["data"]["reports"]}
            assert not first_ids.intersection(report["report_uid"] for report in second["data"]["reports"])

            filters = {"target_names": ["삼성전자"], "report_types": ["company"]}
            companies = await call("list_reports", {"filters": filters, "limit": 2})
            assert companies["data"]["total_count"] > 0, "Samsung reports are needed for the filtered smoke scenario"
            evidence["filtered_report_count"] = companies["data"]["total_count"]
            search = await call("search_reports", {
                "query": "삼성전자 메모리 반도체 실적 전망과 주요 위험 요인",
                "filters": filters, "top_k": 3,
            })
            hits = search["data"]["results"]
            assert hits and all(hit["target_name"] == "삼성전자" for hit in hits)
            assert all(hit["excerpt"] for hit in hits)
            evidence["filtered_search"] = {
                "hit_count": len(hits),
                "top_source": {key: hits[0][key] for key in ("report_uid", "title", "broker", "report_date")},
                "excerpt_preview": hits[0]["excerpt"][:240],
            }
            uid = hits[0]["report_uid"]
            read_args = {"report_uid": uid, "expected_revision": search["meta"]["revision"]}
            page = await call("read_report", {**read_args, "max_chars": 300})
            assert len(page["data"]["content"]) == 300 and page["meta"]["next_cursor"]
            next_page = await call("read_report", {**read_args, "max_chars": 300, "cursor": page["meta"]["next_cursor"]})
            complete_window = await call("read_report", {**read_args, "max_chars": 600})
            assert page["data"]["content"] + next_page["data"]["content"] == complete_window["data"]["content"]
            evidence["body_pagination"] = {"report_uid": uid, "verified_chars": 600}

            broad = await call("search_reports", {"query": "반도체 HBM 수요와 설비투자 전망", "top_k": 3})
            assert broad["data"]["results"], "Broad semantic search returned no hits"
            evidence["broad_search"] = [{key: hit[key] for key in ("title", "target_name", "report_date")} for hit in broad["data"]["results"]]
            await call("list_reports", {"filters": {"report_date_start": "2026-09-19", "report_date_end": "2026-09-01"}}, "INVALID_ARGUMENT")
            await call("read_report", {"report_uid": "0" * 64}, "REPORT_NOT_FOUND")
            final = await call("get_status", {})
            assert final["meta"]["revision"] == revision, "Corpus changed during the test"
            evidence["corpus_revision_unchanged"] = True
    evidence["shutdown"] = "clean"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Local JSON evidence path; contains source metadata, never API keys.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    evidence = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "calls": [], "passed": False}
    try:
        asyncio.run(exercise(root, evidence))
        evidence["passed"] = True
    except Exception as exc:
        evidence["failure_type"] = type(exc).__name__
        # Protocol errors may include arbitrary external payloads; do not persist them.
        if isinstance(exc, AssertionError):
            evidence["failure"] = str(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": evidence["passed"], "evidence": str(args.output)}, ensure_ascii=False), flush=True)
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
