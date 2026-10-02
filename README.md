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

## How it works

```
FastAPI app ──① RouterScanner introspects app.routes ──▶ GraphQL schema
                   (GET → Query field; POST/PUT/PATCH/DELETE → Mutation field)
              ◀──② RouteInvoker (httpx ASGITransport + lifespan)
              Agent via MCP: get_schema + graphql_query(/graphql_mutation)
```

- **Zero decoration**: routes, params and `response_model` are read from your app.
- **Auth preserved**: queries run through the real ASGI app, so `Depends`/middleware
  apply. Pass credentials via `headers_provider`.
- **Composable**: agents pick fields and combine routes in one GraphQL query.

Status: P1 (core + simple MCP mode). See docs/changelog.md.
