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

## How it works

```
FastAPI app ──① RouterScanner introspects app.routes ──▶ GraphQLSchema (graphql-core)
                   GET → Query field; POST/PUT/PATCH/DELETE → Mutation field
                   path/query/body params → args; response_model → output type
              ◀──② RouteInvoker (httpx ASGITransport + asgi-lifespan)
              Agent via MCP: get_schema + graphql_query(/graphql_mutation)
```

| HTTP | GraphQL |
|---|---|
| `GET /items` | `get_items(limit: Int = 10): [ItemOut!]` |
| `GET /items/{item_id}` | `get_items_by_item_id(item_id: Int!): ItemOut` |
| `POST /items` (mutation enabled) | `create_items(payload: ItemCreateInput!): ItemOut` |

Rules worth knowing:

- **Mutations are off by default** (`allow_mutation=True` to expose writes);
  `graphql_query` also refuses mutation documents.
- Untyped routes (no `response_model`/return annotation, raw `Response`,
  hidden routes, required header/cookie params) are **skipped with a warning**.
- `include`/`exclude` fnmatch globs scope which routes enter the schema.
- Route tags form a **domain tree** (`tags=["billing:invoice"]`); progressive
  disclosure tooling over it lands in P2.
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

### Mounted into the same app

```python
mcp.mount_to(app, "/mcp")   # http://host/mcp for remote MCP clients
```

### Plain GraphQL (no MCP)

```python
from routerql import RouterGraphQLHandler

handler = RouterGraphQLHandler(app)
print(handler.get_sdl())
result = await handler.execute(
    "query($id: Int!) { get_users_by_user_id(user_id: $id) { name } }",
    variables={"id": 1},
)
```

## Demo

```bash
uv run --extra mcp python -m demo.run_mcp                          # stdio MCP
uv run --extra mcp uvicorn demo.run_http:app --port 8010           # HTTP + /mcp
```

## Development

```bash
uv sync && uv run pytest        # 125 tests
uv run ruff check src tests
uv run mypy src
```

## Status

P1 (core + simple MCP mode). Roadmap: P2 tag-based progressive disclosure
(`list_domains` / `list_queries` / `get_query_schema`), BaseModel query
parameter models, GraphiQL page; P3 mutation whitelisting; P4 PyPI release.

Design extracted from [nexusx](https://github.com/KLR-Pattern/nexusx)
(SQLModel → GraphQL → MCP), rebuilt on graphql-core standard execution.

## License

MIT
