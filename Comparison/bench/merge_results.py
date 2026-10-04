"""Merge both halves into results.json + print a readable summary.

Run from bench/:  python3 merge_results.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent


def tok(b: int) -> int:
    return b // 4


def main() -> None:
    ours = json.loads((HERE / "results_ours.json").read_text())
    theirs = json.loads((HERE / "results_theirs.json").read_text())

    results = {
        "method": {
            "hardware": "macOS, Apple Silicon (this dev machine)",
            "date": "2026-10-04",
            "catalog": "JSON bytes of the tools/list payload; tokens = bytes/4",
            "latency": "in-memory MCP sessions, 200 iters (50 for composition)",
            "envs": "two separate venvs — fastmcp 4 requires mcp>=2, "
            "fastapi-mcp 0.4.0 breaks on mcp 2.x (Server signature change)",
        },
        "catalog": [],
        "composition": {"ours": ours["composition"], "theirs": theirs["composition"]},
        "latency": {"ours": ours["latency"], "theirs": theirs["latency"]},
        "response_size": {"ours": ours["response_size"], "theirs": theirs["response_size"]},
        "error_semantics": {"ours": ours["error_semantics"], "theirs": theirs["error_semantics"]},
        "fidelity": {
            "ours_sdl_fragment": ours["fidelity_sdl_fragment"],
            "theirs_tool_description": theirs["fidelity_tool_description"],
            "theirs_input_schema": theirs["fidelity_input_schema"],
        },
    }

    for o, t in zip(ours["catalog"], theirs["catalog"], strict=True):
        assert o["routes"] == t["routes"]
        row = {
            "routes": o["routes"],
            "theirs": t | {"catalog_tokens": tok(t["catalog_bytes"])},
            "ours_simple": o["ours_simple"]
            | {
                "full_context_tokens": tok(o["ours_simple"]["full_context_bytes"]),
                "catalog_tokens": tok(o["ours_simple"]["catalog_bytes"]),
            },
            "ours_progressive": o["ours_progressive"]
            | {
                "discovery_total_tokens": tok(o["ours_progressive"]["discovery_total_bytes"]),
                "catalog_tokens": tok(o["ours_progressive"]["catalog_bytes"]),
            },
        }
        results["catalog"].append(row)

    (HERE / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))

    print(f"{'routes':>6} | {'theirs tools':>12} {'tokens':>8} | "
          f"{'ours(simple) tokens*':>21} | {'ours(progr) tokens**':>20}")
    for r in results["catalog"]:
        print(
            f"{r['routes']:>6} | {r['theirs']['tool_count']:>12} "
            f"{r['theirs']['catalog_tokens']:>8} | "
            f"{r['ours_simple']['full_context_tokens']:>21} | "
            f"{r['ours_progressive']['discovery_total_tokens']:>20}"
        )
    print("*  tools/list + get_schema (whole API in context)")
    print("** tools/list + full progressive walk for ONE domain (notes)")
    print()
    print("composition:", json.dumps(results["composition"], ensure_ascii=False))
    print("latency:    ", json.dumps(results["latency"], ensure_ascii=False))
    print("response:   ", json.dumps(results["response_size"], ensure_ascii=False))


if __name__ == "__main__":
    main()
