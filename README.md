# fastapi-gql-mcp

Turn any FastAPI router into a **GraphQL query layer + MCP server** — zero decorators,
zero model changes.

```python
from fastapi import FastAPI
from fastapi_gql_mcp import RouterMCP

app = FastAPI()

# ... your existing routes ...

mcp = RouterMCP(app, name="my-app")
mcp.run()  # HTTP MCP server with get_schema + graphql_query tools
```

**Contents** — [Why](#why) · [How it works](#how-it-works) ·
[Installation](#installation) · [Usage](#usage) · [Authentication](#authentication) ·
[Observability](#observability-opentelemetry) · [Hardening](#hardening-the-bridge) ·
[Demo](#demo) · [Development](#development) · [Status](#status)

## Why

Existing FastAPI→MCP bridges map **one tool per endpoint**: dozens of tools, no
composition, whole-payload responses. fastapi-gql-mcp instead derives a **GraphQL
schema** from your routes (Apollo's "GraphQL as the MCP contract" pattern), so
agents get:

- **a constant tool set** (2 in simple mode, up to 6 with progressive
  disclosure) — never one tool per endpoint
- **field-level selection** — fetch `{ id name }`, not the whole payload
- **composition** — combine several routes in one query; a failing route nulls
  only its own field
- **your docs, verbatim** — docstrings and `description=` metadata travel into
  the schema the agent reads
- **real auth** — queries run through the actual ASGI app, so `Depends`,
  middleware and headers apply; pass credentials via per-caller header
  passthrough

### Compared to the alternatives

| Project | Tool count | Field selection | Composition | Setup |
|---|---|---|---|---|
| [fastapi-mcp](https://github.com/tadata-org/fastapi_mcp) (Tadata) | one per endpoint | ✗ | ✗ | none |
| [FastMCP.from_openapi](https://gofastmcp.com/servers/openapi) | one per endpoint | ✗ | ✗ | none |
| **fastapi-gql-mcp** | 2-6, constant | ✓ | ✓ | none |

Full head-to-head — context-growth curves, latency, auth models, selection
guidance, all measured on one shared app: [Comparison](./Comparison/README.md).

## How it works

No decorators, no model changes — everything is derived from the app you
already have, in three steps:

1. **Scan** — `RouterScanner` reads `app.routes`: verb, path, params,
   `response_model`, tags, docstrings.
2. **Build** — a graphql-core schema is assembled: tags become domain groups,
   endpoint function names become field names, Pydantic models become GraphQL
   types, and your documentation becomes schema descriptions.
3. **Execute** — each field's resolver calls its route in-process through the
   real ASGI app, so `Depends`, middleware and auth behave exactly as over
   HTTP. Sibling fields resolve concurrently; a failing route nulls only its
   own field.

One route, end to end:

```python
# your code — unchanged
@app.get("/products", response_model=list[ProductOut], tags=["shop:catalog"])
async def list_products(filters: Annotated[ProductFilter, Query()]) -> list[ProductOut]:
    """Browse the product catalog."""
```

```graphql
# the schema the agent discovers (excerpt)
type Query { shop: ShopQuery! }
type ShopQuery { catalog: ShopCatalogQuery! }
type ShopCatalogQuery {
  list_products(category: String, in_stock: Boolean, limit: Int = 10): [ProductOut!]
}

# what the agent asks — field-level selection, routes combined in one query
{
  shop     { catalog { list_products(in_stock: true) { name } } }
  analytics { shop_stats { revenue_cents } }
}
```

The shape follows two rules:

- **Tags group the tree** — `tags=["shop:catalog"]` answers at
  `{ shop { catalog { … } } }`. Untagged routes join the domain of their first
  path segment, so every field has a group.
- **Function names name the leaves** — `async def list_products` becomes
  `list_products`, your own vocabulary with no URL reconstruction. Two routes
  sharing a function name fail fast with `DuplicateFieldError`.

Rules worth knowing:

- **Mutations are off by default** (`allow_mutation=True` to expose writes;
  `mutation_include=[...]` globs to whitelist specific write routes);
  `graphql_query` also refuses mutation documents.
- **Mutation ordering**: per the GraphQL spec, mutation fields at the ROOT
  execute serially in declaration order — with the grouped schema that means
  writes in DIFFERENT domains are ordered; writes grouped under the SAME
  domain run in parallel like query fields. When write order matters, put the
  operations in separate domains or send separate mutation documents.
- **Dynamic shapes pass through as `JSON`** — endpoints and fields annotated
  `dict`, `dict[K, V]` or `Any` bridge as the `JSON` scalar instead of being
  skipped (both directions: a `JSON` argument lands as the raw request body).
  Routes with NO annotation and no `response_model`, raw `Response` returns,
  hidden routes and required header/cookie params are still **skipped with a
  warning** — `handler.skips` lists them programmatically, so CI can assert
  nothing fell out of the schema unnoticed.
- `include`/`exclude` fnmatch globs scope which routes enter the schema.
- Route tags form a **domain tree** (`tags=["billing:invoice"]`); large apps
  switch to **progressive disclosure** (below).
- A lone `Annotated[FilterModel, Query()]` flattens into individual query
  arguments (FastAPI Query Parameter Models).
- Same-named Pydantic classes from different modules get qualified type names.
- **Descriptions flow into the schema** — model docstrings → type
  descriptions, `Field(description=...)` → field descriptions, endpoint
  docstrings (or `summary=`) → field descriptions,
  `Query()/Path()/Body(description=...)` → argument descriptions. They surface
  in GraphiQL hover, introspection and every MCP discovery tool.

## Installation

Requires Python >= 3.10.

```bash
uv add fastapi-gql-mcp            # core: GraphQL handler
uv add 'fastapi-gql-mcp[mcp]'     # + MCP server (fastmcp)
```

## Usage

### MCP server (HTTP)

`run()` serves streamable HTTP (the only transport — the wrapped app is a
service, and per-caller credential passthrough needs an HTTP request
context). Use `mount_to(app, "/mcp")` to serve MCP on the app's own port.

```python
mcp = RouterMCP(
    app,
    name="my-app",
    allow_mutation=False,
    include=["/api/*"],
    # The caller's own Authorization header travels to the routes by default;
    # an empty list disables forwarding entirely.
    # passthrough_headers=["authorization"],
)
mcp.run()  # streamable HTTP, 127.0.0.1:8000 — mcp.run(host="0.0.0.0", port=9000)
```

### Progressive disclosure (large apps)

Above `progressive_threshold` routes (default 25, `mode="auto"`), the toolset
switches to a 4-layer walkthrough of the tag tree:

```
list_domains ──▶ list_queries("billing:invoice") ──▶ get_query_schema("billing:invoice") ──▶ graphql_query
                     (list_mutations with allow_mutation=True)
```

Each domain SDL fragment re-wraps the real group types along the path, so it
shows **exactly the grouped query the agent must write** — nothing more, and
with every description attached. **Discovery is scoped; execution is not**:
`graphql_query` always runs against the full schema, so fields from different
domains combine freely. Force either mode with `mode="simple" | "progressive"`.

### Mounted into the same app

```python
mcp.mount_to(app, "/mcp")            # streamable HTTP at /mcp/
mcp.handler.mount_graphql(app)       # GraphiQL at /graphiql + POST /graphql
```

### Plain GraphQL (no MCP)

```python
from fastapi_gql_mcp import RouterGraphQLHandler

handler = RouterGraphQLHandler(app)
print(handler.get_sdl())
result = await handler.execute(
    "query($id: Int!) { iam { get_user(user_id: $id) { name } } }",
    variables={"id": 1},
)
```

## Authentication

Route calls travel through the real ASGI app in-process, so `Depends`,
middleware and security schemes behave exactly as over HTTP. Credentials have
a **single source: the caller** — the FastAPI security schemes are the only
verifiers, and this bridge never holds or manages tokens of its own.

- **Per-caller passthrough (default)**: each MCP/GraphQL client connects with
  its own credentials and `passthrough_headers` (default
  `("authorization",)`) forwards them to the routes — queries run as the
  caller, exactly as they would over HTTP. An explicitly empty list disables
  forwarding; headers are matched case-insensitively and only whitelisted
  names ever reach a route (no smuggling `x-internal-token` past the bridge).
- **Without credentials, protected routes fail** — field errors like
  `HTTP_401` in query results; nothing falls back to a server-side identity.
- **Machines without a user context** configure the service credential on the
  MCP client side (or, for programmatic use, pass
  `handler.execute(..., headers={...})` directly).
- **MCP endpoint OAuth (optional)**: pass a fastmcp auth provider —
  `auth=GitHubProvider(client_id=..., client_secret=..., base_url=...)` — and
  the MCP endpoint speaks OAuth 2.1: 401 discovery, dynamic client
  registration, PKCE, a consent page, and its own reference tokens verifying
  every call. Claude Code opens a browser, the user logs in, and the agent's
  queries run as that user. `mount_to(app, "/mcp", auth_at_root=True)` hosts
  the OAuth routes at the app root (for reusing an IdP app whose registered
  callback lives there). The bridge itself still verifies nothing. Full
  wired flow: [examples/notes_oauth](./examples/notes_oauth/).

Expose the MCP endpoint only behind an entrance you control (network, or a
FastAPI `Depends` on the mounted route) — the bridge authenticates no one
itself, and combine with `allow_mutation=False` / `mutation_include` to keep
writes out of reach.

## Observability (OpenTelemetry)

Install an OpenTelemetry SDK next to your app — that's the whole setup. The
spans are emitted natively from both ends, and the bridge stitches them into
one waterfall:

- **fastmcp** emits the tool level (`tools/call graphql_query`);
- the bridge emits `graphql.execute` (the GraphQL orchestration layer) and
  injects W3C `traceparent` into every in-process route call — independent
  of `passthrough_headers`, a no-op without an SDK (`opentelemetry-api`
  only, non-recording by default);
- **FastAPI >= 0.142** emits the route level (`GET /things` plus
  `fastapi.dependencies/endpoint/serialization`) and extracts the injected
  context — so route spans nest under `graphql.execute`, one trace per
  query.

Route-call timeouts and concurrency queue waits surface as span events
(`route.timeout`, `route.queue`) on `graphql.execute`. A runnable proof
(plus the Jaeger walkthrough): [examples/otel_smoke.md](./examples/otel_smoke.md);
a live wired app: [examples/notes_oauth](./examples/notes_oauth) (env-gated
`app/observability.py`). Metrics (per-URL QPS/p99) are out of scope here —
derive them from spans with an OTel Collector `spanmetrics` connector.

## Hardening the bridge

Three knobs are built in and on by default:

- **`request_timeout`** (default 30s, `None` disables) — per-route-call
  deadline. The in-process ASGI call bypasses httpx's own timeout machinery,
  so enforcement lives in `asyncio.wait_for`; a timed-out field surfaces as a
  `TIMEOUT` error (http_status 504) while its siblings survive.
- **`max_depth`** (default 10, `None` disables) — maximum selection-set
  nesting per document. Recursive models make depth unbounded and an MCP
  caller is an LLM that can emit runaway nesting; overly deep documents are
  rejected with a validation-style error before anything executes.
- **`max_concurrency`** (default 16, `None` disables) — bound on in-flight
  route calls across all queries. Sibling fields resolve concurrently, so
  one wide query fans out; this protects the wrapped app's upstream from
  being hammered by its own bridge (queueing counts against
  `request_timeout`, default 30s).

All three are parameters of `RouterGraphQLHandler` and `RouterMCP`. For anything policy-shaped, `validation_rules=` on the handler
passes extra graphql-core validation rules through.

For rate limiting and response caps on the **MCP face**, FastMCP's
middleware suite attaches with zero bridge code — `RouterMCP.mcp` is the
underlying `FastMCP` instance:

```python
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware

mcp = RouterMCP(app)
mcp.mcp.add_middleware(RateLimitingMiddleware(max_requests_per_second=10))
mcp.mcp.add_middleware(ResponseLimitingMiddleware(max_size=1_000_000))
```

`RateLimitingMiddleware` limits **per client** by default (pass
`get_client_id=` to customize the key or `global_limit=True` for a shared
bucket); `ResponseLimitingMiddleware` truncates oversized tool responses
(default 1 MB, configurable suffix). The `POST /graphql` face does not go
through fastmcp — attach your own middleware to the host app for that
endpoint.

## Demo

The `demo/` directory runs a small shop app (users / catalog / orders / stats,
auth via `x-token: demo-secret`) with every feature in play — including **full
documentation coverage** so all four description chains are inspectable in
GraphiQL:

```bash
uv run --extra mcp python -m demo               # REST + /mcp/ + /graphiql + /graphql on :8010
uv run --extra mcp python -m demo.mcp_walkthrough  # agent's-eye MCP walkthrough, no client needed
```

`python -m demo` prints all endpoint URLs and serves the grouped schema;
`/now` is untyped on purpose so the skip warning is visible at startup.

For the full consumer experience — a real app with **GitHub OAuth login,
session cookies, and MCP OAuth (Claude Code's browser login flow)** — see
[`examples/notes_oauth`](./examples/notes_oauth/): three interchangeable
credential carriers resolved in one place, the MCP endpoint protected by
an OAuth 2.1 proxy, and a smoke script that walks the protected paths
headlessly. For observability, [examples/otel_smoke.md](./examples/otel_smoke.md)
walks the one-waterfall-per-query proof in Jaeger.

## Development

```bash
uv sync && uv run pytest        # tests
uv run ruff check src tests
uv run mypy src
```

## Status

0.4.0 — see [CHANGELOG.md](CHANGELOG.md). Ideas welcome: GraphQL subscriptions
over SSE routes, response header pass-through, per-domain auth scopes.

Design extracted from [nexusx](https://github.com/KLR-Pattern/nexusx)
(SQLModel → GraphQL → MCP), rebuilt on graphql-core standard execution.

## License

MIT
