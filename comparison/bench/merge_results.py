"""Merge both halves into results.json + print a readable summary.

Run from bench/:  python3 merge_results.py
"""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent


def tok(b: int) -> int:
    return b // 4


def cpu_name() -> str:
    if platform.system() == "Darwin":
        return subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True,
        ).stdout.strip()
    # Linux: first Model line from /proc/cpuinfo
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def main() -> None:
    ours = json.loads((HERE / "results_ours.json").read_text())
    theirs = json.loads((HERE / "results_theirs.json").read_text())

    import sys

    results = {
        "method": {
            "hardware": cpu_name(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "date": str(date.today()),
            "versions": {
                # recorded by each runner inside its own venv
                "ours": ours["versions"],
                "theirs": theirs["versions"],
            },
            "catalog": "JSON bytes of the tools/list payload (full model_dump both sides); tokens = bytes/4",
            "latency": "in-memory MCP sessions, 200 iters x 3 runs, medians reported; "
            "both sides driven by the fastmcp in-memory Client (same stack)",
            "envs": "two separate venvs for process isolation; both on fastmcp 4",
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
