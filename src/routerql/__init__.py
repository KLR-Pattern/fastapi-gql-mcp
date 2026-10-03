"""routerql: turn any FastAPI router into a GraphQL query layer + MCP server."""

from routerql.handler import RouterGraphQLHandler, RouterQLConfigError
from routerql.invoker import HeadersProvider, RouteInvoker
from routerql.mcp.errors import RouterQLErrors
from routerql.mcp.server import RouterMCP
from routerql.scanner import RouteInfo, RouterScanner

__version__ = "0.3.0"

__all__ = [
    "HeadersProvider",
    "RouteInfo",
    "RouteInvoker",
    "RouterGraphQLHandler",
    "RouterMCP",
    "RouterQLConfigError",
    "RouterQLErrors",
    "RouterScanner",
    "__version__",
]
