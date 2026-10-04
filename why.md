# Why fastapi-gql-mcp

The problem, the timing, and the strategy behind this repo. Every number
below is measured and reproducible in [comparison/](./comparison/) —
including the ones where the competing approach wins.

## The origin

We maintain FastAPI services, and MCP is how AI agents connect to software.
So we did what everyone does: we wired our app to an MCP server with an
existing bridge and pointed an agent at it. With five endpoints, it worked.
With a real service — fifty-plus routes — the agent started every task by
swallowing a phone book of tool definitions, then walked the API one
endpoint per turn. The problem was never FastAPI and never MCP. It was the
shape of the thing between them.

## What "FastAPI → MCP" bridges objectively get wrong today

Measured head-to-head against the 12k-star incumbent
([fastapi-mcp](https://github.com/tadata-org/fastapi_mcp)), same app wired
into both:

1. **Context grows linearly with your API.** One tool per endpoint costs
   ~110 tokens per endpoint of tool catalog. At 100 routes that is ~11,000
   tokens burned before the agent does anything — every session.
2. **No composition.** One call = one endpoint. A view that needs "notes +
   stats + who am I" costs three tool calls — three LLM turns at seconds
   each. Turns, not milliseconds, are an agent's real latency.
3. **Whole payloads.** No field selection: the same list costs 4.7× more
   bytes than asking for two fields.
4. **All-or-nothing errors.** One failing endpoint fails the entire call;
   the healthy results die with it.
5. **Ungated writes and borrowed credentials.** Everything is writable by
   default, and server-side bridges tend to hold one service token — the
   agent stops acting as the user.

To be fair: below ~10 endpoints, the one-tool-per-endpoint catalog is
actually *smaller* than ours (685 vs 893 tokens). The failure is
specifically at scale — which is where real services live.

## Why now

Three things just lined up:

1. **Agents became real clients of real APIs.** 2025–2026 is when MCP went
   from a protocol to the default way models touch software — Claude Code,
   IDE agents, and chat clients all speak it natively now. Every team with
   a FastAPI service is about to face the bridging question.
2. **Context is still the scarce resource.** Windows grow, but every token
   of tool catalog is paid again on every session — and priced per token.
   Context economics is not a temporary problem; it is the new bandwidth.
3. **The contract shape is still unsettled.** The spec's own answer to
   catalog bloat — the Tool Search extension — is a draft that needs both
   client and server support. For a narrow window, *how a large API should
   enter an agent's context* is an open question with no default answer.

FastAPI is the dominant Python API framework, and the incumbent bridges
have already shipped the one-tool-per-endpoint answer. The measured
alternative needs to exist **now**, while teams are still choosing.

## The GraphQL insight

GraphQL already solved this exact problem — ten years ago, for human
clients. Facebook built it because mobile clients on slow links needed to
ask a large API for *exactly* what they needed: typed, composable,
selectable, in one round trip, with partial results when part of the
request fails. An AI agent is that client again, in a sharper form: the
context window is the small screen, and every tool call is a round trip.
Apollo named this pattern "GraphQL as the MCP contract." Nobody had made
it a one-liner for the framework half the Python backend world already
runs.

## The strategy: derive, don't decorate

**1. Derive the contract from what exists.** A scanner reads your routes —
tags become a domain tree (`shop:catalog` → `{ shop { catalog { … } } }`),
endpoint function names become field names, your docstrings and
`Field(description=…)` become the schema the agent reads. Zero decorators,
zero model changes: `RouterMCP(app)` is the whole setup.

**2. Keep the tool set constant.** Two to six tools, forever. The schema is
the contract; large apps switch to progressive disclosure (domains →
queries → SDL fragment) and context stays ~1,500 tokens no matter how big
the API grows.

**3. Execute through the real app, as the caller.** Every field's resolver
calls its route in-process through the actual ASGI app, so `Depends`,
middleware and auth behave exactly as over HTTP. Credentials have one
source — the caller — forwarded per-request; the bridge holds no tokens.
Writes are off by default with a whitelist to open them.

**4. Ship it production-grade.** Full OAuth 2.1 on the MCP endpoint
(Claude Code opens a browser, logs in, and every query runs as that user),
timeout/concurrency/depth guards, and OpenTelemetry tracing that stitches
one waterfall per query — tool span, GraphQL span, route spans in a single
trace.

**Where it stands:** v0.4.0 on PyPI, 228 tests, CI across Python
3.10–3.14, mypy-strict, and a runnable end-to-end example with GitHub
OAuth that we use as the acceptance test.

## Project status — early, deliberately transparent

This is an early-stage project (0.x), and we treat that as a feature of
the process, not a weakness of the design:

- Everything claimed above is **measured and reproducible** — the
  comparison, the benchmarks, and the OAuth example all live in the repo,
  including the numbers where the competing approach wins.
- The API surface is still small and **shaped by feedback**: the JSON
  pass-through, the OAuth endpoint gating, and the depth-guard fix all
  landed within days of real users hitting those exact edges.
- **Issues and suggestions are welcome, and they get acted on** — bring us
  your API's shape and we will either make it work or tell you honestly
  why another tool fits better.

We would rather earn trust with runnable evidence and fast iteration than
with roadmap slides.

> **One line:** MCP gave agents a way to connect; GraphQL gives them a
> contract worth connecting to. fastapi-gql-mcp is the missing three lines
> of code in between — early, measured, and actively maintained.
