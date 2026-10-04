"""The non-MCP entry point: a GraphQL handler over a FastAPI app.

Constructing the handler scans the app and builds the schema eagerly, so
configuration problems surface at startup, not at first query.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from graphql import GraphQLSchema, graphql, print_schema

from fastapi_gql_mcp.depth_guard import depth_error
from fastapi_gql_mcp.invoker import InvocationContext, RouteInvoker
from fastapi_gql_mcp.scanner import RouteInfo, RouterScanner, SkipRecord
from fastapi_gql_mcp.schema_builder import GQLMCPConfigError, SchemaBuilder
from fastapi_gql_mcp.type_builder import TypeBuilder


class RouterGraphQLHandler:
    """Exposes a FastAPI app as an executable GraphQL schema."""

    def __init__(
        self,
        app: Any,
        *,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        allow_mutation: bool = False,
        include_hidden: bool = False,
        mutation_include: Sequence[str] | None = None,
        passthrough_headers: Sequence[str] | None = None,
        request_timeout: float | None = 30.0,
        max_concurrency: int | None = 16,
        max_depth: int | None = 10,
        validation_rules: Sequence[Any] | None = None,
    ) -> None:
        if max_depth is not None and max_depth < 1:
            raise ValueError("max_depth must be >= 1, or None to disable")
        self._invoker = RouteInvoker(
            app, timeout=request_timeout, max_concurrency=max_concurrency
        )
        self._max_depth = max_depth
        self._validation_rules = tuple(validation_rules) if validation_rules else ()
        # Whitelist of inbound header names untrusted callers may forward into
        # route calls, lowercased at construction. None = the default
        # ("authorization",): same-app bridges speak for the caller, so the
        # caller's own credential travels by default. An explicitly empty
        # sequence disables passthrough entirely.
        if passthrough_headers is None:
            self._passthrough_headers: tuple[str, ...] = ("authorization",)
        else:
            self._passthrough_headers = tuple(
                h.strip().lower() for h in passthrough_headers if h.strip()
            )
        self._types = TypeBuilder()
        routes, skips = RouterScanner(
            app,
            include=include,
            exclude=exclude,
            allow_mutation=allow_mutation,
            include_hidden=include_hidden,
            mutation_include=mutation_include,
        ).scan(self._types)
        self._builder = SchemaBuilder(routes, self._invoker, self._types)
        self._schema = self._builder.build()
        self._routes: list[RouteInfo] = routes
        self._skips: list[SkipRecord] = skips

    @property
    def schema(self) -> GraphQLSchema:
        return self._schema

    @property
    def routes(self) -> list[RouteInfo]:
        return list(self._routes)

    @property
    def skips(self) -> list[SkipRecord]:
        """Routes excluded from the schema, with reasons — the scanner's
        report, so callers can assert nothing disappeared unexpectedly
        (e.g. a new endpoint silently failing to map in CI)."""
        return list(self._skips)

    @property
    def invoker(self) -> RouteInvoker:
        return self._invoker

    @property
    def passthrough_headers(self) -> tuple[str, ...]:
        """Lowercased header names callers may forward (default: authorization only)."""
        return self._passthrough_headers

    @property
    def query_fields(self) -> dict[tuple[tuple[str, ...], str], Any]:
        """(domain path, field name) -> GraphQLField index (the schema's leaves)."""
        return self._builder.query_fields

    @property
    def mutation_fields(self) -> dict[tuple[tuple[str, ...], str], Any]:
        return self._builder.mutation_fields

    def get_sdl(self) -> str:
        """Full schema in SDL form (the MCP ``get_schema`` payload)."""
        return print_schema(self._schema)

    async def execute(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the app's routes.

        ``headers`` are TRUSTED per-call headers — the single credential
        channel (there is no server-side provider). Callers sourcing them
        from an untrusted origin (MCP request, /graphql endpoint) must filter
        them through ``filter_passthrough_headers`` first.
        """
        if self._max_depth is not None:
            error = depth_error(query, self._max_depth)
            if error is not None:
                return {"errors": [error.formatted]}
        await self._invoker.start()
        result = await graphql(
            self._schema,
            query,
            variable_values=variables or {},
            operation_name=operation_name,
            context_value=InvocationContext(headers=headers),
            rules=self._validation_rules or None,
        )
        payload: dict[str, Any] = {}
        if result.data is not None:
            payload["data"] = result.data
        if result.errors:
            payload["errors"] = [error.formatted for error in result.errors]
        return payload

    async def aclose(self) -> None:
        """Release the invoker's HTTP client and app lifespan."""
        await self._invoker.aclose()

    def mount_graphql(
        self,
        app: Any,
        *,
        graphql_path: str = "/graphql",
        graphiql_path: str = "/graphiql",
    ) -> None:
        """Serve a GraphiQL playground + GraphQL HTTP endpoint on a FastAPI app.

        Mounting into the SAME app that fastapi-gql-mcp wraps disables the invoker's
        lifespan management (the app's own server lifespan drives it once).
        """
        from fastapi_gql_mcp.http_api import create_graphql_router

        if app is self._invoker.app:
            self._invoker.disable_lifespan_management()
        router = create_graphql_router(
            self, graphql_path=graphql_path, graphiql_path=graphiql_path
        )
        app.include_router(router)


__all__ = ["RouterGraphQLHandler", "GQLMCPConfigError"]
