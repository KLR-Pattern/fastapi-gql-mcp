"""RouterMCP: the one-line MCP server over a FastAPI app."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Literal

from fastapi import FastAPI

from fastapi_gql_mcp.domains import DomainRegistry
from fastapi_gql_mcp.handler import GQLMCPConfigError, RouterGraphQLHandler
from fastapi_gql_mcp.mcp.tools import register_simple_tools

logger = logging.getLogger(__name__)

# Route count above which "auto" mode prefers progressive disclosure (P2).
PROGRESSIVE_THRESHOLD = 25


def _compose_lifespan(host: FastAPI, sub_http_app: Any) -> None:
    """Chain a mounted sub-app's lifespan into the host app's lifespan.

    Starlette never runs lifespans of mounted apps, but fastmcp's streamable
    HTTP session manager requires one ("Task group is not initialized").
    """
    from collections.abc import AsyncIterator
    from contextlib import AsyncExitStack, asynccontextmanager

    host_lifespan = host.router.lifespan_context
    sub_lifespan = getattr(sub_http_app, "lifespan", None) or sub_http_app.router.lifespan_context

    @asynccontextmanager
    async def combined(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(host_lifespan(app))
            await stack.enter_async_context(sub_lifespan(app))
            yield

    host.router.lifespan_context = combined


class RouterMCP:
    """Expose a FastAPI app as an MCP server backed by a GraphQL schema.

    Args:
        app: The FastAPI application whose routes become the schema.
        name: MCP server name shown to clients.
        include/exclude: fnmatch globs over route paths (exclude wins).
        allow_mutation: Expose POST/PUT/PATCH/DELETE routes as GraphQL
            mutations. Default False (read-only).
        mutation_include: fnmatch globs limiting WHICH write routes become
            mutations (requires allow_mutation=True).
        passthrough_headers: Whitelist of inbound header names (case
            insensitive) MCP/GraphQL callers may forward into route calls.
            ``None`` (default) forwards ``authorization`` — each client acts
            as its own JWT user, the FastAPI security schemes doing the
            verifying. Pass ``[]`` to disable forwarding entirely. With no
            HTTP request context (in-memory client) nothing is forwarded:
            protected routes answer 401. Protocol headers (``content-type``
            / ``accept``) are refused even if whitelisted — the invoker
            owns them, and a forwarded value would retype the JSON request.
        request_timeout: Per-route-call timeout in seconds (default 30,
            ``None`` disables). Enforced with asyncio.wait_for — httpx's
            own timeout is inert on the in-process ASGI transport.
        auth: Optional ``fastmcp`` auth provider (e.g.
            ``fastmcp.server.auth.providers.github.GitHubProvider``). Passed
            through to ``FastMCP`` untouched: the MCP endpoint then answers
            401 with OAuth discovery metadata, and clients' Bearer tokens
            reach the routes via ``passthrough_headers`` like any other
            caller's. The bridge itself verifies nothing.
        mode: ``simple`` registers get_schema + graphql_query;
            ``progressive`` registers the 4-layer tag-based disclosure
            (list_domains -> list_queries -> get_query_schema ->
            graphql_query); ``auto`` picks progressive once the schema
            carries more than ``progressive_threshold`` routes (counted
            after include/exclude filtering).
        include_hidden: Also scan routes with ``include_in_schema=False``.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        name: str = "fastapi-gql-mcp API",
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        allow_mutation: bool = False,
        mode: Literal["auto", "simple", "progressive"] = "auto",
        include_hidden: bool = False,
        progressive_threshold: int = PROGRESSIVE_THRESHOLD,
        mutation_include: Sequence[str] | None = None,
        passthrough_headers: Sequence[str] | None = None,
        auth: Any | None = None,
        request_timeout: float | None = 30.0,
    ) -> None:
        self._mode = mode
        self._progressive_threshold = progressive_threshold
        self._handler = RouterGraphQLHandler(
            app,
            include=include,
            exclude=exclude,
            allow_mutation=allow_mutation,
            include_hidden=include_hidden,
            mutation_include=mutation_include,
            passthrough_headers=passthrough_headers,
            request_timeout=request_timeout,
        )
        self._resolved_mode = self._resolve_mode(mode)
        self._domains = DomainRegistry(self._handler.routes)
        self._mcp = self._build_mcp(name, allow_mutation, auth)

    def _resolve_mode(
        self, mode: Literal["auto", "simple", "progressive"]
    ) -> Literal["simple", "progressive"]:
        if mode == "simple":
            return "simple"
        if mode == "progressive":
            return "progressive"
        # auto: progressive disclosure pays off once the SDL is too big to
        # hand an agent in one shot. Count the routes that ENTER the schema
        # (include/exclude applied, include_router wrappers resolved by the
        # scanner) — raw app.routes undercounts on FastAPI >= 0.142, where
        # include_router results are wrapped in non-APIRoute objects, and
        # overcounts when include/exclude narrows the schema.
        route_count = len(self._handler.routes)
        if route_count > self._progressive_threshold:
            logger.info(
                "Schema carries %d routes (> %d): using progressive disclosure",
                route_count,
                self._progressive_threshold,
            )
            return "progressive"
        return "simple"

    @property
    def mode(self) -> Literal["simple", "progressive"]:
        return self._resolved_mode

    def _build_mcp(self, name: str, allow_mutation: bool, auth: Any | None = None) -> Any:
        # fastmcp is an optional extra; import lazily like nexusx does.
        from fastmcp import FastMCP

        mcp = FastMCP(name, auth=auth)
        if self._resolved_mode == "progressive":
            from fastapi_gql_mcp.mcp.progressive_tools import register_progressive_tools

            register_progressive_tools(
                mcp, self._handler, self._domains, allow_mutation=allow_mutation
            )
        else:
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

    def run(self, *, host: str = "127.0.0.1", port: int = 8000) -> None:
        """Run the MCP server over streamable HTTP.

        HTTP is the only transport: the wrapped app is a service whose routes
        speak HTTP, and per-caller credential passthrough needs the HTTP
        request context that stdio has no notion of. Use ``mount_to`` to
        serve MCP on the app's own port instead of opening a second one.
        """
        self._mcp.run(transport="http", host=host, port=port)

    def mount_to(
        self, app: FastAPI, path: str = "/mcp", *, auth_at_root: bool = False
    ) -> None:
        """Mount the MCP server into a FastAPI app (streamable HTTP).

        The endpoint is served at ``{path}/`` — e.g. ``/mcp/`` by default.
        Mounting into the SAME app that fastapi-gql-mcp wraps disables the invoker's
        lifespan management (the app's own uvicorn lifespan drives it once).
        Mounting into a different app leaves it managed, but that app will not
        run the wrapped app's startup hooks — prefer mounting into the same app.

        ``auth_at_root`` (requires ``auth=``): serve the MCP endpoint at
        ``path`` but host the auth provider's routes — OAuth endpoints,
        consent page, ``/.well-known/*`` discovery — at the host app's ROOT
        instead of under the mount. Use this when the upstream IdP's
        registered callback lives at the root domain (e.g. reusing an OAuth
        app whose callback is ``/auth/callback`` via a ``redirect_path``
        subdirectory).
        """
        if auth_at_root and getattr(self._mcp, "auth", None) is None:
            raise GQLMCPConfigError(
                "auth_at_root requires RouterMCP(..., auth=...) — no auth provider set"
            )
        if auth_at_root:
            http_app = self._mcp.http_app(path=path)
            if app is self._handler.invoker.app:
                self._handler.invoker.disable_lifespan_management()
            else:
                logger.warning(
                    "Mounting fastapi-gql-mcp into a different app than the one it wraps: "
                    "the wrapped app's lifespan events will not fire unless "
                    "something else runs them."
                )
            _compose_lifespan(app, http_app)
            # No Mount("") here: it is a catch-all, so any host route added
            # AFTER this call would silently 404 behind it. Instead, splice
            # the routes (appended last — host routes keep precedence) and
            # copy the sub-app's whole middleware stack.
            #
            # fastmcp private surface this relies on — VERIFY EACH on any
            # fastmcp major bump (pyproject pins <5 for exactly this):
            #   1. http_app.routes carrying a per-route RequireAuthMiddleware
            #      guard on the MCP endpoint;
            #   2. app-level AuthenticationMiddleware (verify_token setting
            #      scope["user"]) on the sub-app's user_middleware stack;
            #   3. RequestContextMiddleware (captures inbound headers so
            #      tool calls can forward them), also from user_middleware;
            #   4. auth.get_well_known_routes() returning re-exposable
            #      /.well-known/* routes.
            # Dropping any of these fails soft-but-visible: 401s, or
            # per-caller headers silently stop reaching the routes.
            for route in http_app.routes:
                app.router.routes.append(route)
            for middleware in getattr(http_app, "user_middleware", []):
                app.add_middleware(middleware.cls, **(middleware.kwargs or {}))
            return
        if app is self._handler.invoker.app:
            # Same app: its server lifespan already (or will) run; a second
            # LifespanManager would drive startup twice.
            self._handler.invoker.disable_lifespan_management()
        else:
            logger.warning(
                "Mounting fastapi-gql-mcp into a different app than the one it wraps: "
                "the wrapped app's lifespan events will not fire unless "
                "something else runs them."
            )
        # http_app's internal route defaults to "/mcp"; re-root it to "/" so the
        # mount path itself is the endpoint.
        http_app = self._mcp.http_app(path="/")
        # Starlette does NOT run lifespans of mounted sub-apps; fastmcp's
        # streamable HTTP session manager needs one, so compose it into the
        # host app's lifespan.
        _compose_lifespan(app, http_app)
        app.mount(path, http_app)
        # OAuth discovery (RFC 9728): the 401 challenge advertises the
        # protected-resource metadata at a HOST-ROOT well-known URL, but the
        # mount above shifts the auth routes under `path` — re-expose the
        # well-known ones at the root where clients expect them.
        auth = getattr(self._mcp, "auth", None)
        if auth is not None:
            existing = {getattr(r, "path", None) for r in app.router.routes}
            for route in auth.get_well_known_routes():
                if route.path not in existing:
                    app.router.routes.append(route)
