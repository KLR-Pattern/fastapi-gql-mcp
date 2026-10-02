"""Assemble the final ``GraphQLSchema`` from scanned routes.

Every Query/Mutation field's resolver delegates to ``RouteInvoker.invoke``;
GraphQL-level nullability mirrors FastAPI's required flags so a request the
schema accepts is a request FastAPI will not 422.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from graphql import (
    GraphQLArgument,
    GraphQLEnumType,
    GraphQLField,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    Undefined,
)

from routerql.invoker import RouteInvoker
from routerql.naming import validate_field_names
from routerql.scanner import ParamInfo, RouteInfo
from routerql.type_builder import TypeBuilder

logger = logging.getLogger(__name__)


class RouterQLConfigError(ValueError):
    """Raised when the scanned routes cannot form a usable schema."""


class DuplicateArgError(ValueError):
    """Two parameters of one route map onto the same GraphQL argument name."""


def _argument(param: ParamInfo, types: TypeBuilder, route: RouteInfo) -> GraphQLArgument:
    bare = cast(
        "GraphQLScalarType | GraphQLEnumType | GraphQLInputObjectType | GraphQLList[Any]",
        types.input_type(param.annotation, context=f"parameter '{param.name}' of {route.path}"),
    )
    gtype: Any = GraphQLNonNull(bare) if param.required else bare
    default = Undefined if param.required else _graphql_default(param.default)
    return GraphQLArgument(gtype, default_value=default)


def _graphql_default(default: Any) -> Any:
    if default is None or isinstance(default, bool | int | float | str):
        return default
    return Undefined


def _arguments(
    route: RouteInfo, types: TypeBuilder
) -> dict[str, GraphQLArgument]:
    args: dict[str, GraphQLArgument] = {}
    for param in (*route.path_params, *route.query_params, *route.body_params):
        if param.name in args:
            raise DuplicateArgError(
                f"Route {route.method} {route.path} has two parameters mapping to "
                f"GraphQL argument '{param.name}'; rename one of them."
            )
        args[param.name] = _argument(param, types, route)
    return args


def _resolver(
    route: RouteInfo, invoker: RouteInvoker
) -> Any:
    async def resolve(_root: Any, _info: Any, **kwargs: Any) -> Any:
        return await invoker.invoke(route, kwargs)

    return resolve


def _fields(
    routes: Sequence[RouteInfo], invoker: RouteInvoker, types: TypeBuilder
) -> dict[str, GraphQLField]:
    fields: dict[str, GraphQLField] = {}
    for route in routes:
        # Route responses are deliberately NULLABLE at the field level: a route
        # erroring (4xx/5xx) must null only its own field, so agents composing
        # several routes in one query keep the other results. A NonNull field
        # error would null the whole response per GraphQL spec.
        fields[route.field_name] = GraphQLField(
            types.bare_output_type(
                route.response_annotation, context=f"response of {route.path}"
            ),
            args=_arguments(route, types),
            resolve=_resolver(route, invoker),
            description=route.description,
        )
    return fields


class SchemaBuilder:
    """Builds a ``GraphQLSchema`` from ``RouteInfo`` items."""

    def __init__(
        self,
        routes: Sequence[RouteInfo],
        invoker: RouteInvoker,
        types: TypeBuilder | None = None,
    ) -> None:
        self._routes = list(routes)
        self._invoker = invoker
        self._types = types or TypeBuilder()

    @property
    def types(self) -> TypeBuilder:
        return self._types

    def build(self) -> GraphQLSchema:
        query_routes = [r for r in self._routes if not r.is_mutation]
        mutation_routes = [r for r in self._routes if r.is_mutation]

        if not query_routes and not mutation_routes:
            raise RouterQLConfigError(
                "routerql found no routable endpoints: every route was skipped "
                "(see the routerql warning log for reasons)."
            )

        validate_field_names([(r.field_name, r.method, r.path) for r in query_routes])
        if mutation_routes:
            validate_field_names(
                [(r.field_name, r.method, r.path) for r in mutation_routes]
            )

        query_type = GraphQLObjectType(
            name="Query",
            fields=lambda: _fields(query_routes, self._invoker, self._types),
        )
        mutation_type = None
        if mutation_routes:
            mutation_type = GraphQLObjectType(
                name="Mutation",
                fields=lambda: _fields(mutation_routes, self._invoker, self._types),
            )
        return GraphQLSchema(query=query_type, mutation=mutation_type)
