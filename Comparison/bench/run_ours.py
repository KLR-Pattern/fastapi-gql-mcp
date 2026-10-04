"""fastapi-gql-mcp half of the benchmark. Writes results_ours.json.

Run from bench/:  uv run --project env_ours python run_ours.py
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

from fastmcp import Client

from fastapi_gql_mcp import RouterMCP

from shared_app import NOTES, build_app

OUT = Path(__file__).parent / "results_ours.json"

NOTES_QUERY = (
    '{ notes { mine { list_notes(q: "note 1") { id title body owner } } } '
    "meta { stats { notes owners } } }"
)
NOTES_QUERY_FULL = "{ notes { mine { list_notes { id title body owner } } } }"
NOTES_QUERY_PROJECTED = "{ notes { mine { list_notes { id title } } } }"


def json_size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode())


def pct(sorted_ms: list[float], p: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * p))]


async def catalog_for(route_count: int) -> dict:
    app = build_app(route_count)
    row: dict = {"routes": route_count}

    simple = RouterMCP(app, name="bench", allow_mutation=True, mode="simple")
    async with Client(simple.mcp) as client:
        tools = await client.list_tools()
        payload = {
            "tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in tools
            ]
        }
        sdl = json.loads((await client.call_tool("get_schema", {})).content[0].text)["data"]["sdl"]
    row["ours_simple"] = {
        "tool_count": len(tools),
        "catalog_bytes": json_size(payload),
        "sdl_bytes": len(sdl.encode()),
    }
    row["ours_simple"]["full_context_bytes"] = (
        row["ours_simple"]["catalog_bytes"] + row["ours_simple"]["sdl_bytes"]
    )

    progressive = RouterMCP(app, name="bench", allow_mutation=True, mode="progressive")
    async with Client(progressive.mcp) as client:
        tools = await client.list_tools()
        payload = {
            "tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in tools
            ]
        }
        domains = json.loads((await client.call_tool("list_domains", {})).content[0].text)
        domain = next(d["name"] for d in domains["data"]["domains"] if d["name"] == "notes")
        queries = json.loads(
            (await client.call_tool("list_queries", {"domain": domain})).content[0].text
        )
        frag = json.loads(
            (await client.call_tool("get_query_schema", {"domain": domain})).content[0].text
        )
    row["ours_progressive"] = {
        "tool_count": len(tools),
        "catalog_bytes": json_size(payload),
        "discovery_domains_bytes": json_size(domains),
        "discovery_list_bytes": json_size(queries),
        "discovery_fragment_bytes": json_size(frag),
    }
    row["ours_progressive"]["discovery_total_bytes"] = (
        row["ours_progressive"]["catalog_bytes"]
        + row["ours_progressive"]["discovery_domains_bytes"]
        + row["ours_progressive"]["discovery_list_bytes"]
        + row["ours_progressive"]["discovery_fragment_bytes"]
    )
    return row


async def main() -> dict:
    results: dict = {}

    print("== catalog ==")
    results["catalog"] = [await catalog_for(n) for n in [5, 10, 25, 50, 100]]

    app = build_app(5)
    simple = RouterMCP(app, name="bench", allow_mutation=True, mode="simple")

    print("== composition ==")
    async with Client(simple.mcp) as client:
        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            await client.call_tool("graphql_query", {"query": NOTES_QUERY})
            times.append((time.perf_counter() - t0) * 1000)
    results["composition"] = {
        "tool_calls": 1,
        "mean_ms": round(statistics.mean(times), 2),
        "p95_ms": round(pct(sorted(times), 0.95), 2),
    }

    print("== latency ==")
    async with Client(simple.mcp) as client:
        times = []
        for _ in range(200):
            t0 = time.perf_counter()
            await client.call_tool("graphql_query", {"query": NOTES_QUERY_FULL})
            times.append((time.perf_counter() - t0) * 1000)
    s = sorted(times)
    results["latency"] = {
        "p50_ms": round(pct(s, 0.50), 2),
        "p95_ms": round(pct(s, 0.95), 2),
        "mean_ms": round(statistics.mean(times), 2),
    }

    print("== response size ==")
    async with Client(simple.mcp) as client:
        full = (await client.call_tool("graphql_query", {"query": NOTES_QUERY_FULL})).content[0].text
        projected = (await client.call_tool("graphql_query", {"query": NOTES_QUERY_PROJECTED})).content[0].text
    results["response_size"] = {
        "note_count": len(NOTES),
        "full_bytes": len(full.encode()),
        "projected_bytes": len(projected.encode()),
    }

    print("== error semantics ==")
    async with Client(simple.mcp) as client:
        res = json.loads(
            (
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ meta { stats { notes } } "
                        "notes { mine { get_note(note_id: 9999) { id } } } }"
                    },
                )
            ).content[0].text
        )
    results["error_semantics"] = res

    print("== fidelity ==")
    async with Client(simple.mcp) as client:
        sdl = json.loads((await client.call_tool("get_schema", {})).content[0].text)["data"]["sdl"]
    results["fidelity_sdl_fragment"] = sdl[
        sdl.index("type NotesMineQuery") : sdl.index("type NoteOut")
    ].strip()

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"wrote {OUT}")
    return results


if __name__ == "__main__":
    asyncio.run(main())
