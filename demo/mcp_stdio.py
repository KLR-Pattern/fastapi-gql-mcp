"""Run the demo app as a stdio MCP server.

    uv run --extra mcp python -m demo.mcp_stdio

Point any stdio MCP client at this command, e.g. in Claude Desktop /
Claude Code MCP config:

    {"mcpServers": {"routerql-demo": {"command": "uv",
                                      "args": ["run", "--extra", "mcp",
                                               "python", "-m", "demo.mcp_stdio"],
                                      "cwd": "/path/to/routerql"}}}
"""

from demo.app import DEMO_TOKEN, create_app
from routerql import RouterMCP

mcp = RouterMCP(
    create_app(),
    name="routerql demo",
    allow_mutation=True,
    headers_provider=lambda: {"x-token": DEMO_TOKEN},
)

if __name__ == "__main__":
    mcp.run()
