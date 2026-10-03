# fastapi-gql-mcp

Turn any FastAPI router into a **GraphQL query layer + MCP server** — zero decorators,
zero model changes.

```python
from fastapi import FastAPI
from fastapi_gql_mcp import RouterMCP

app = FastAPI()

# ... your existing routes ...

mcp = RouterMCP(app, name="my-app")
mcp.run()  # stdio MCP server with get_schema + graphql_query tools
```

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
  middleware and headers apply; pass credentials via `headers_provider`

### Compared to the alternatives

The other FastAPI→MCP bridges map endpoints to tools one-to-one. fastapi-gql-mcp
instead derives a GraphQL schema from your routes — GraphQL is the
implementation vehicle, the contract the agent sees: few constant tools,
field-level selection and cross-endpoint composition for free.

| Project | Tool count | Field selection | Composition | Setup |
|---|---|---|---|---|
| [fastapi-mcp](https://github.com/tadata-org/fastapi_mcp) (Tadata) | one per endpoint | ✗ | ✗ | none |
| [FastMCP.from_openapi](https://gofastmcp.com/servers/openapi) | one per endpoint | ✗ | ✗ | none |
| **fastapi-gql-mcp** | 2-6, constant | ✓ | ✓ | none |

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
- Untyped routes (no `response_model`/return annotation, raw `Response`,
  hidden routes, required header/cookie params) are **skipped with a warning**.
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

```bash
uv add fastapi-gql-mcp            # core: GraphQL handler
uv add 'fastapi-gql-mcp[mcp]'     # + MCP server (fastmcp)
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
auth via `x-token: demo-secret`) with every feature in play — including **full
documentation coverage** so all four description chains are inspectable in
GraphiQL:

```bash
uv run --extra mcp python -m demo               # REST + /mcp/ + /graphiql + /graphql on :8010
uv run --extra mcp python -m demo.mcp_stdio     # stdio MCP server (Claude Desktop etc.)
uv run --extra mcp python -m demo.mcp_walkthrough  # agent's-eye MCP walkthrough, no client needed
```

`python -m demo` prints all endpoint URLs and serves the grouped schema;
`/now` is untyped on purpose so the skip warning is visible at startup.

## Development

```bash
uv sync && uv run pytest        # tests
uv run ruff check src tests
uv run mypy src
```

## Status

0.3.0 — see [CHANGELOG.md](CHANGELOG.md). Ideas welcome: GraphQL subscriptions
over SSE routes, response header pass-through, per-domain auth scopes.

Design extracted from [nexusx](https://github.com/KLR-Pattern/nexusx)
(SQLModel → GraphQL → MCP), rebuilt on graphql-core standard execution.

## License

MIT
