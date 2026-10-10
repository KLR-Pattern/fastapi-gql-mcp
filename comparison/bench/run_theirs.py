"""FastMCP.from_openapi half of the benchmark. Writes results_theirs.json.

Run from bench/:  uv run --project env_theirs python run_theirs.py

The "theirs" side is fastmcp's OpenAPI bridge — the mainstream way to expose
an existing API as MCP tools today. The app's own OpenAPI spec feeds
``FastMCP.from_openapi``; tool calls ride an in-process ASGI httpx client,
so both sides measure the same in-process shape (no network either way).
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx
from fastmcp import Client, FastMCP

from shared_app import NOTES, build_app

OUT = Path(__file__).parent / "results_theirs.json"


def json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode())


def pct(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * p))]


def openapi_bridge(app) -> FastMCP:
    """The app's OpenAPI spec -> one MCP tool per operation."""
    return FastMCP.from_openapi(
        openapi_spec=app.openapi(),
        client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bench"
        ),
        name="bench-openapi",
    )


async def catalog_for(route_count: int) -> dict:
    app = build_app(route_count)
    mcp = openapi_bridge(app)
    async with Client(mcp) as client:
        tools = await client.list_tools()
    payload = {"tools": [t.model_dump(exclude_none=True) for t in tools]}
    return {
        "routes": route_count,
        "tool_count": len(tools),
        "catalog_bytes": json_size(payload),
    }


async def main() -> dict:
    import importlib.metadata as md

    results: dict = {
        "versions": {
            name: md.version(name)
            for name in ("fastmcp", "fastapi", "httpx")
        }
    }

    print("== catalog ==")
    results["catalog"] = [await catalog_for(n) for n in [5, 10, 25, 50, 100]]

    app = build_app(5)
    mcp = openapi_bridge(app)

    print("== composition ==")
    run_means = []
    for _ in range(3):
        async with Client(mcp) as client:
            tools = await client.list_tools()
            names = {t.name: t for t in tools}
            list_notes = next(n for n in names if n.startswith("list_notes"))
            stats = next(n for n in names if n.startswith("stats"))
            times = []
            for _ in range(50):
                t0 = time.perf_counter()
                await client.call_tool(list_notes, {"q": "note 1"})
                await client.call_tool(stats, {})
                times.append((time.perf_counter() - t0) * 1000)
        run_means.append(round(statistics.mean(times), 2))
    results["composition"] = {
        "tool_calls": 2,
        "runs": 3,
        "mean_ms_per_run": run_means,
        "mean_ms": round(statistics.median(run_means), 2),
    }

    print("== latency ==")
    runs = []
    for _ in range(3):
        async with Client(mcp) as client:
            tools = await client.list_tools()
            list_notes = next(t.name for t in tools if t.name.startswith("list_notes"))
            times = []
            for _ in range(200):
                t0 = time.perf_counter()
                await client.call_tool(list_notes, {})
                times.append((time.perf_counter() - t0) * 1000)
        runs.append(sorted(times))

    def med(q):
        return round(statistics.median(pct(r, q) for r in runs), 2)

    results["latency"] = {
        "runs": 3,
        "p50_ms": med(.5),
        "p95_ms": med(.95),
        "spread_p50_ms": [round(pct(r, .5), 2) for r in runs],
    }

    print("== response size ==")
    async with Client(mcp) as client:
        tools = await client.list_tools()
        list_notes = next(t.name for t in tools if t.name.startswith("list_notes"))
        result = await client.call_tool(list_notes, {})
    text = result.content[0].text if result.content else ""
    results["response_size"] = {"note_count": len(NOTES), "full_bytes": len(text.encode())}

    print("== error semantics ==")
    async with Client(mcp) as client:
        tools = await client.list_tools()
        get_note = next(t.name for t in tools if t.name.startswith("get_note"))
        try:
            await client.call_tool(get_note, {"note_id": 9999})
            results["error_semantics"] = {"raises": False, "text": ""}
        except Exception as exc:  # fastmcp raises ToolError on HTTP >= 400
            results["error_semantics"] = {"raises": True, "text": str(exc)[:400]}

    print("== fidelity ==")
    async with Client(mcp) as client:
        tools = await client.list_tools()
        tool = next(t for t in tools if t.name.startswith("list_notes"))
    results["fidelity_tool_description"] = tool.description
    results["fidelity_input_schema"] = tool.input_schema

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"wrote {OUT}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
