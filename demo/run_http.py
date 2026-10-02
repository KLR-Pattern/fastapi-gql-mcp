"""Serve the demo app over HTTP with routerql mounted at /mcp.

    uv run --extra mcp uvicorn demo.run_http:app --port 8010 --reload

- MCP streamable HTTP:  http://127.0.0.1:8010/mcp/
- GraphiQL playground:  http://127.0.0.1:8010/graphiql
- GraphQL HTTP:         POST http://127.0.0.1:8010/graphql
"""

from routerql import RouterMCP
from demo.demo_app import app

mcp = RouterMCP(
    app,
    name="shop-demo",
    allow_mutation=True,
    headers_provider=lambda: {"x-token": "demo-secret"},
    include=["/products*", "/orders*"],
)
mcp.mount_to(app, "/mcp")
mcp.handler.mount_graphql(app)  # /graphiql + POST /graphql
