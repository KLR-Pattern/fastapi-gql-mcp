"""Run the demo: ``uv run python -m app`` (reads .env, serves on :8020)."""

from __future__ import annotations

import uvicorn

from app.config import BASE_URL, mcp_oauth_configured, oauth_configured


def main() -> None:
    host = BASE_URL.split("//", 1)[-1]
    port = int(host.split(":")[1]) if ":" in host else 8020
    print(
        "\n  fastapi-gql-mcp demo\n"
        "  ─────────────────────────────────────────────────────\n"
        f"  Home / login:           http://localhost:{port}/auth/\n"
        f"  REST docs:              http://localhost:{port}/docs\n"
        f"  GraphiQL:               http://localhost:{port}/graphiql\n"
        f"  MCP (streamable HTTP):  http://localhost:{port}/mcp\n"
        "  ─────────────────────────────────────────────────────\n"
        f"  GitHub OAuth configured:  {oauth_configured()}\n"
        f"  MCP OAuth login enabled:  {mcp_oauth_configured()}\n"
        "  (Ctrl-C to stop)\n",
    )
    uvicorn.run("app.main:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
