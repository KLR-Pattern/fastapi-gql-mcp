"""The non-MCP entry point: a GraphQL handler over a FastAPI app.

Constructing the handler scans the app and builds the schema eagerly, so
configuration problems surface at startup, not at first query.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graphql import GraphQLSchema, graphql, print_schema

from routerql.invoker import HeadersProvider, RouteInvoker
from routerql.scanner import RouteInfo, RouterScanner
from routerql.schema_builder import RouterQLConfigError, SchemaBuilder
from routerql.type_builder import TypeBuilder


class RouterGraphQLHandler:
    """Exposes a FastAPI app as an executable GraphQL schema."""

    def __init__(
        self,
        app: Any,
        *,
        include: Sequence[str] | None = None,
        exclude: Sequence[str] | None = None,
        allow_mutation: bool = False,
        headers_provider: HeadersProvider | None = None,
        include_hidden: bool = False,
    ) -> None:
        self._invoker = RouteInvoker(app, headers_provider=headers_provider)
        self._types = TypeBuilder()
        routes, _skips = RouterScanner(
            app,
            include=include,
            exclude=exclude,
            allow_mutation=allow_mutation,
            include_hidden=include_hidden,
        ).scan(self._types)
        self._schema = SchemaBuilder(routes, self._invoker, self._types).build()
        self._routes: list[RouteInfo] = routes

    @property
    def schema(self) -> GraphQLSchema:
        return self._schema

    @property
    def routes(self) -> list[RouteInfo]:
        return list(self._routes)

    @property
    def invoker(self) -> RouteInvoker:
        return self._invoker

    def get_sdl(self) -> str:
        """Full schema in SDL form (the MCP ``get_schema`` payload)."""
        return print_schema(self._schema)

    async def execute(
        self,
        query: str,
        *,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the app's routes."""
        await self._invoker.start()
        result = await graphql(
            self._schema,
            query,
            variable_values=variables or {},
            operation_name=operation_name,
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


__all__ = ["RouterGraphQLHandler", "RouterQLConfigError"]
