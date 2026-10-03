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
from fastapi_gql_mcp import RouterMCP

app = create_app()

mcp = RouterMCP(
    app,
    name="fastapi-gql-mcp demo",
    allow_mutation=True,
    # The demo app has 13 routes (< the 25-route threshold), so "auto" would
    # pick simple mode (get_schema + graphql_query). Force progressive to
    # expose the tag-based disclosure tools:
    # list_domains -> list_queries/list_mutations -> get_query_schema -> graphql_query
    mode="progressive",
    headers_provider=lambda: {"x-token": DEMO_TOKEN},
)
mcp.mount_to(app, "/mcp")
mcp.handler.mount_graphql(app)
