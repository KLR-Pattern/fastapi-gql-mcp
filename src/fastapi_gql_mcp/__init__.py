"""fastapi-gql-mcp: turn any FastAPI router into a GraphQL query layer + MCP server."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from fastapi_gql_mcp.handler import GQLMCPConfigError, RouterGraphQLHandler
from fastapi_gql_mcp.invoker import RouteInvoker, filter_passthrough_headers
from fastapi_gql_mcp.mcp.errors import GQLMCPErrors
from fastapi_gql_mcp.mcp.server import FastAPIMCP, RouterMCP
from fastapi_gql_mcp.scanner import (
    DegradedRecord,
    ReadinessReport,
    RouteInfo,
    RouterScanner,
    SkipRecord,
)

# Derived from installed metadata (pyproject is the single source of truth).
# A hardcoded copy here already drifted once (0.4.0 while shipping 0.5.0);
# deriving removes the release-step class of bug entirely.
try:
    __version__ = _pkg_version("fastapi-gql-mcp")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0.0.0.dev0"

__all__ = [
    "FastAPIMCP",
    "RouterMCP",  # deprecated alias, remove at 1.0
    "DegradedRecord",
    "ReadinessReport",
    "RouteInfo",
    "RouteInvoker",
    "RouterGraphQLHandler",
    "GQLMCPConfigError",
    "GQLMCPErrors",
    "RouterScanner",
    "SkipRecord",
    "filter_passthrough_headers",
    "__version__",
]
