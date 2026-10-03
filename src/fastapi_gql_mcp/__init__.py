"""fastapi-gql-mcp: turn any FastAPI router into a GraphQL query layer + MCP server."""

from fastapi_gql_mcp.handler import GQLMCPConfigError, RouterGraphQLHandler
from fastapi_gql_mcp.invoker import HeadersProvider, RouteInvoker
from fastapi_gql_mcp.mcp.errors import GQLMCPErrors
from fastapi_gql_mcp.mcp.server import RouterMCP
from fastapi_gql_mcp.scanner import RouteInfo, RouterScanner

__version__ = "0.3.0"

__all__ = [
    "HeadersProvider",
    "RouteInfo",
    "RouteInvoker",
    "RouterGraphQLHandler",
    "RouterMCP",
    "GQLMCPConfigError",
    "GQLMCPErrors",
    "RouterScanner",
    "__version__",
]
