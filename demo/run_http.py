"""Serve the demo app over HTTP with routerql mounted at /mcp.

    uv run --extra mcp uvicorn demo.run_http:app --port 8010 --reload

Then point an MCP client at http://127.0.0.1:8010/mcp, or try the plain
GraphQL handler from a script (see README).
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
