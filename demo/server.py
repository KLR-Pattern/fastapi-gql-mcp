"""The all-mounted demo server: REST + MCP + GraphiQL + GraphQL on one app.

Run with ``uv run --extra mcp python -m demo`` (or
``uv run --extra mcp uvicorn demo.server:app --port 8010``).

Endpoints:
- REST (OpenAPI docs):  http://127.0.0.1:8010/docs
- MCP streamable HTTP:  http://127.0.0.1:8010/mcp/
- GraphiQL playground:  http://127.0.0.1:8010/graphiql
- GraphQL HTTP:         POST http://127.0.0.1:8010/graphql
"""

from demo.app import DEMO_TOKEN, create_app
from routerql import RouterMCP

app = create_app()

mcp = RouterMCP(
    app,
    name="routerql demo",
    allow_mutation=True,
    headers_provider=lambda: {"x-token": DEMO_TOKEN},
)
mcp.mount_to(app, "/mcp")
mcp.handler.mount_graphql(app)
