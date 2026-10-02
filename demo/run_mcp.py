"""Run the demo app as a stdio MCP server.

    uv run --extra mcp python -m demo.run_mcp
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

if __name__ == "__main__":
    mcp.run()
