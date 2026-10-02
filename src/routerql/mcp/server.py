"""RouterMCP: the one-line MCP server over a FastAPI app."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import FastAPI

from routerql.domains import DomainRegistry
from routerql.handler import RouterGraphQLHandler
from routerql.invoker import HeadersProvider
from routerql.mcp.tools import register_simple_tools

logger = logging.getLogger(__name__)

# Route count above which "auto" mode prefers progressive disclosure (P2).
PROGRESSIVE_THRESHOLD = 25

_Transport = Literal["stdio", "http"]


class RouterMCP:
    """Expose a FastAPI app as an MCP server backed by a GraphQL schema.

    Args:
        app: The FastAPI application whose routes become the schema.
        name: MCP server name shown to clients.
        include/exclude: fnmatch globs over route paths (exclude wins).
        allow_mutation: Expose POST/PUT/PATCH/DELETE routes as GraphQL
            mutations. Default False (read-only).
        headers_provider: Callable (sync or async) returning headers merged
            into every route call — inject credentials here.
        mode: ``simple`` registers get_schema + graphql_query; ``progressive``
            (tag-based disclosure) lands in P2; ``auto`` currently resolves to
            ``simple``.
        include_hidden: Also scan routes with ``include_in_schema=False``.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        name: str = "routerql API",
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        allow_mutation: bool = False,
        headers_provider: HeadersProvider | None = None,
        mode: Literal["auto", "simple", "progressive"] = "auto",
        include_hidden: bool = False,
    ) -> None:
        self._mode = mode
        resolved = self._resolve_mode(mode, app)
        self._handler = RouterGraphQLHandler(
            app,
            include=include,
            exclude=exclude,
            allow_mutation=allow_mutation,
            headers_provider=headers_provider,
            include_hidden=include_hidden,
        )
        self._domains = DomainRegistry(self._handler.routes)
        self._mcp = self._build_mcp(name, resolved, allow_mutation)

    @staticmethod
    def _resolve_mode(
        mode: Literal["auto", "simple", "progressive"], app: FastAPI
    ) -> Literal["simple", "progressive"]:
        if mode == "progressive":
            raise NotImplementedError(
                "progressive disclosure (list_domains / list_queries / "
                "get_query_schema) lands in P2"
            )
        if mode == "auto":
            from fastapi.routing import APIRoute

            route_count = sum(1 for r in app.routes if isinstance(r, APIRoute))
            if route_count > PROGRESSIVE_THRESHOLD:
                logger.info(
                    "App has %d routes (> %d): progressive disclosure would be "
                    "beneficial; using simple mode until P2 lands",
                    route_count,
                    PROGRESSIVE_THRESHOLD,
                )
            return "simple"
        return "simple"

    def _build_mcp(
        self, name: str, resolved: Literal["simple", "progressive"], allow_mutation: bool
    ) -> Any:
        # fastmcp is an optional extra; import lazily like nexusx does.
        from fastmcp import FastMCP

        mcp = FastMCP(name)
        register_simple_tools(mcp, self._handler, allow_mutation=allow_mutation)
        return mcp

    @property
    def handler(self) -> RouterGraphQLHandler:
        return self._handler

    @property
    def domains(self) -> DomainRegistry:
        return self._domains

    @property
    def mcp(self) -> Any:
        return self._mcp

    def run(self, *, transport: _Transport = "stdio") -> None:
        """Run the MCP server (stdio by default, http for remote clients)."""
        self._mcp.run(transport=transport)

    def mount_to(self, app: FastAPI, path: str = "/mcp") -> None:
        """Mount the MCP server into a FastAPI app.

        Mounting into the SAME app that routerql wraps disables the invoker's
        lifespan management (the app's own uvicorn lifespan drives it once).
        Mounting into a different app leaves it managed, but that app will not
        run the wrapped app's startup hooks — prefer mounting into the same app.
        """
        if app is self._handler.invoker.app:
            # Same app: its server lifespan already (or will) run; a second
            # LifespanManager would drive startup twice.
            self._handler.invoker.disable_lifespan_management()
        else:
            logger.warning(
                "Mounting routerql into a different app than the one it wraps: "
                "the wrapped app's lifespan events will not fire unless "
                "something else runs them."
            )
        app.mount(path, self._mcp.http_app())
