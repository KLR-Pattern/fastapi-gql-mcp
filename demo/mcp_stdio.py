"""Run the demo app as a stdio MCP server.

    uv run --extra mcp python -m demo.mcp_stdio

Point any stdio MCP client at this command, e.g. in Claude Desktop /
Claude Code MCP config:

    {"mcpServers": {"fastapi-gql-mcp-demo": {"command": "uv",
                                      "args": ["run", "--extra", "mcp",
                                               "python", "-m", "demo.mcp_stdio"],
                                      "cwd": "/path/to/fastapi-gql-mcp"}}}
"""

from demo.app import DEMO_TOKEN, create_app
from fastapi_gql_mcp import RouterMCP

mcp = RouterMCP(
    create_app(),
    name="fastapi-gql-mcp demo",
    allow_mutation=True,
    headers_provider=lambda: {"x-token": DEMO_TOKEN},
)

if __name__ == "__main__":
    mcp.run()
