# routerql

Turn any FastAPI router into a **GraphQL query layer + MCP server** — zero decorators,
zero model changes.

```python
from fastapi import FastAPI
from routerql import RouterMCP

app = FastAPI()

# ... your existing routes ...

mcp = RouterMCP(app, name="my-app")
mcp.run()  # stdio MCP server with get_schema + graphql_query tools
```

## Why

Existing FastAPI→MCP bridges map **one tool per endpoint**: dozens of tools, no
composition, whole-payload responses. routerql instead derives a **GraphQL
schema** from your routes (Apollo's "GraphQL as the MCP contract" pattern), so
agents get:

- **2-3 constant tools** — `get_schema`, `graphql_query` (+ `graphql_mutation`)
- **field-level selection** — fetch `{ id name }`, not the whole payload
- **composition** — combine several routes in one query; a failing route nulls
  only its own field
- **real auth** — queries run through the actual ASGI app, so `Depends`,
  middleware and headers apply; pass credentials via `headers_provider`

### Compared to the alternatives

| | fastapi-mcp / FastMCP.from_openapi | Apollo MCP (GraphQL ops) | **routerql** |
|---|---|---|---|
| Requires existing GraphQL API | no | **yes** | no — derived from routes |
| Tool count | one per endpoint (grows) | one per operation | 2-6, constant |
| Field-level selection | no | yes | yes |
| Combine endpoints in one call | no | yes | yes |
| Setup cost | none | build a GraphQL server | none |

## How it works

```
FastAPI app ──① RouterScanner introspects app.routes ──▶ GraphQLSchema (graphql-core)
                   GET → Query field; POST/PUT/PATCH/DELETE → Mutation field
                   path/query/body params → args; response_model → output type
              ◀──② RouteInvoker (httpx ASGITransport + asgi-lifespan)
              Agent via MCP: get_schema + graphql_query(/graphql_mutation)
```

| Endpoint | GraphQL field |
|---|---|
| `async def list_items` on `GET /items` (tag `shop:catalog`) | `shop.catalog.list_items` |
| `async def get_item` on `GET /items/{item_id}` (tag `shop:catalog`) | `shop.catalog.get_item(item_id: Int!): ItemOut` |
| `async def create_item` on `POST /items` (tag `shop:catalog`) | `shop.catalog.create_item(payload: ItemCreateInput!): ItemOut` |

**Fields are grouped by the tag-derived domain tree** (UseCaseService-style
hierarchy): a route tagged `shop:catalog` answers at
`{ shop { catalog { list_products { name } } } }`. Untagged routes fall into
the domain of their first path segment, so every field has a group.

**Leaf field names are the endpoint function names** — the developer's own
vocabulary, no URL reconstruction. Function names are unique only per module,
so two routes sharing a name fail fast with a `DuplicateFieldError` (rename
one function or exclude one route).

Rules worth knowing:

- **Mutations are off by default** (`allow_mutation=True` to expose writes;
  `mutation_include=[...]` globs to whitelist specific write routes);
  `graphql_query` also refuses mutation documents.
- Untyped routes (no `response_model`/return annotation, raw `Response`,
  hidden routes, required header/cookie params) are **skipped with a warning**.
- `include`/`exclude` fnmatch globs scope which routes enter the schema.
- Route tags form a **domain tree** (`tags=["billing:invoice"]`); large apps
  switch to **progressive disclosure** (below).
- A lone `Annotated[FilterModel, Query()]` flattens into individual query
  arguments (FastAPI Query Parameter Models).
- Same-named Pydantic classes from different modules get qualified type names.

## Installation

```bash
uv add routerql            # core: GraphQL handler
uv add 'routerql[mcp]'     # + MCP server (fastmcp)
```

## Usage

### MCP server (stdio)

```python
mcp = RouterMCP(
    app,
    name="my-app",
    allow_mutation=False,
    headers_provider=lambda: {"authorization": "Bearer ..."},  # auth passthrough
    include=["/api/*"],
)
mcp.run()
```

### Progressive disclosure (large apps)

Above `progressive_threshold` routes (default 25, `mode="auto"`), the toolset
switches to a 4-layer walkthrough of the tag tree:

```
list_domains ──▶ list_queries("billing:invoice") ──▶ get_query_schema(...) ──▶ graphql_query
```

Each domain SDL fragment contains only that subtree's operations and the types
they reach. **Discovery is scoped; execution is not** — `graphql_query` always
runs against the full schema, so fields from different domains combine freely.
Force either mode with `mode="simple" | "progressive"`.

### Mounted into the same app

```python
mcp.mount_to(app, "/mcp")            # streamable HTTP at /mcp/
mcp.handler.mount_graphql(app)       # GraphiQL at /graphiql + POST /graphql
```

### Plain GraphQL (no MCP)

```python
from routerql import RouterGraphQLHandler

handler = RouterGraphQLHandler(app)
print(handler.get_sdl())
result = await handler.execute(
    "query($id: Int!) { iam { get_user(user_id: $id) { name } } }",
    variables={"id": 1},
)
```

## Authentication

Route calls travel through the real ASGI app in-process, so `Depends`,
middleware and security schemes behave exactly as over HTTP. Two consequences:

- **Without credentials, protected routes fail** — you will see field errors
  like `HTTP_401` in query results.
- **Provide credentials via `headers_provider`** (sync or async, evaluated per
  request): static tokens, environment lookups, or short-lived tokens from
  your own OAuth client:

  ```python
  async def headers_provider() -> dict[str, str]:
      return {"authorization": f"Bearer {await get_access_token()}"}

  mcp = RouterMCP(app, headers_provider=headers_provider)
  ```

Keep in mind the provider runs with the server's privileges — scope the token
to what the agent should be allowed to do (e.g. read-only), and combine with
`allow_mutation=False` / `mutation_include` to keep writes out of reach.

## Demo

The `demo/` directory runs a small shop app (users / catalog / orders / stats,
auth via `x-token: demo-secret`) with every feature in play:

```bash
uv run --extra mcp python -m demo               # REST + /mcp/ + /graphiql + /graphql on :8010
uv run --extra mcp python -m demo.mcp_stdio     # stdio MCP server (Claude Desktop etc.)
uv run --extra mcp python -m demo.mcp_walkthrough  # agent's-eye MCP walkthrough, no client needed
```

`python -m demo` prints all endpoint URLs; `/now` is untyped on purpose so the
skip warning is visible at startup.

## Development

```bash
uv sync && uv run pytest        # tests
uv run ruff check src tests
uv run mypy src
```

## Status

0.2.0 — see [CHANGELOG.md](CHANGELOG.md). Ideas welcome: GraphQL subscriptions
over SSE routes, response header pass-through, per-domain auth scopes.

Design extracted from [nexusx](https://github.com/KLR-Pattern/nexusx)
(SQLModel → GraphQL → MCP), rebuilt on graphql-core standard execution.

## License

MIT
