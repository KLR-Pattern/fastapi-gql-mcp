"""fastapi-mcp (tadata) half of the benchmark. Writes results_theirs.json.

Run from bench/:  uv run --project env_theirs python run_theirs.py

Client = raw mcp SDK ClientSession over in-memory streams (their stack has
no fastmcp); ours uses fastmcp's in-memory client — both bypass network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
import time
from pathlib import Path

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from fastapi_mcp import FastApiMCP

from shared_app import NOTES, build_app

OUT = Path(__file__).parent / "results_theirs.json"


def json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode())


def pct(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * p))]


@contextlib.asynccontextmanager
async def session(server):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        # mcp 1.x: Server.run is a plain coroutine — drive it in a task
        server_task = asyncio.create_task(
            server.run(
                server_streams[0],
                server_streams[1],
                server.create_initialization_options(),
                raise_exceptions=False,
            )
        )
        try:
            async with ClientSession(client_streams[0], client_streams[1]) as sess:
                await sess.initialize()
                yield sess
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task


def tool_name(mcp: FastApiMCP, prefix: str) -> str:
    """Their names are FastAPI operationIds: list_notes_api_notes_get."""
    return next(t.name for t in mcp.tools if t.name.startswith(prefix))


async def call(sess: ClientSession, name: str, args: dict) -> str:
    result = await sess.call_tool(name, args)
    if result.isError:
        raise RuntimeError(result.content[0].text if result.content else "tool error")
    return result.content[0].text


async def catalog_for(route_count: int) -> dict:
    app = build_app(route_count)
    mcp = FastApiMCP(app)
    payload = {"tools": [t.model_dump(exclude_none=True) for t in mcp.tools]}
    return {
        "routes": route_count,
        "tool_count": len(mcp.tools),
        "catalog_bytes": json_size(payload),
    }


async def main() -> dict:
    results: dict = {}

    print("== catalog ==")
    results["catalog"] = [await catalog_for(n) for n in [5, 10, 25, 50, 100]]

    app = build_app(5)
    mcp = FastApiMCP(app)

    print("== composition ==")
    run_means = []
    for _ in range(3):
        async with session(mcp.server) as sess:
            times = []
            for _ in range(50):
                t0 = time.perf_counter()
                await call(sess, tool_name(mcp, "list_notes"), {"q": "note 1"})
                await call(sess, tool_name(mcp, "stats"), {})
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
        async with session(mcp.server) as sess:
            times = []
            for _ in range(200):
                t0 = time.perf_counter()
                await call(sess, tool_name(mcp, "list_notes"), {})
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
    async with session(mcp.server) as sess:
        text = await call(sess, tool_name(mcp, "list_notes"), {})
    results["response_size"] = {"note_count": len(NOTES), "full_bytes": len(text.encode())}

    print("== error semantics ==")
    async with session(mcp.server) as sess:
        result = await sess.call_tool(tool_name(mcp, "get_note"), {"note_id": 9999})
        if result.isError:
            results["error_semantics"] = {
                "isError": True,
                "text": result.content[0].text[:400] if result.content else "",
            }
        else:
            results["error_semantics"] = {"isError": False, "text": result.content[0].text[:400]}

    print("== fidelity ==")
    tool = next(t for t in mcp.tools if t.name.startswith("list_notes"))
    results["fidelity_tool_description"] = tool.description
    results["fidelity_input_schema"] = tool.inputSchema

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"wrote {OUT}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
