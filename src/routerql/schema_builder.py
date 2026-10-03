"""Assemble the final ``GraphQLSchema`` from scanned routes.

Fields are grouped by the tag-derived domain tree (the UseCaseService-style
hierarchy): a route tagged ``shop:catalog`` lands at
``Query.shop.catalog.<function_name>``. Untagged routes fall into the domain
derived from their first path segment, so every field always has a group.

Every leaf field's resolver delegates to ``RouteInvoker.invoke``; GraphQL-level
nullability mirrors FastAPI's required flags so a request the schema accepts
is a request FastAPI will not 422.
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
    return GraphQLArgument(gtype, default_value=default, description=param.description)


def _graphql_default(default: Any) -> Any:
    if default is None or isinstance(default, bool | int | float | str):
        return default
    return Undefined


def _arguments(route: RouteInfo, types: TypeBuilder) -> dict[str, GraphQLArgument]:
    args: dict[str, GraphQLArgument] = {}
    for param in (*route.path_params, *route.query_params, *route.body_params):
        if param.name in args:
            raise DuplicateArgError(
                f"Route {route.method} {route.path} has two parameters mapping to "
                f"GraphQL argument '{param.name}'; rename one of them."
            )
        args[param.name] = _argument(param, types, route)
    return args


def _resolver(route: RouteInfo, invoker: RouteInvoker) -> Any:
    async def resolve(_root: Any, _info: Any, **kwargs: Any) -> Any:
        return await invoker.invoke(route, kwargs)

    return resolve


def _leaf_field(
    route: RouteInfo, invoker: RouteInvoker, types: TypeBuilder
) -> GraphQLField:
    # Route responses are deliberately NULLABLE at the field level: a route
    # erroring (4xx/5xx) must null only its own field, so agents composing
    # several routes in one query keep the other results. A NonNull field
    # error would null the whole response per GraphQL spec.
    return GraphQLField(
        types.bare_output_type(
            route.response_annotation, context=f"response of {route.path}"
        ),
        args=_arguments(route, types),
        resolve=_resolver(route, invoker),
        description=route.description,
    )


def group_type_name(path: tuple[str, ...], *, mutation: bool) -> str:
    """Deterministic group type name: ``("shop", "catalog")`` -> ShopCatalogQuery."""
    pascal = "".join(part.title() for seg in path for part in seg.split("_"))
    return f"{pascal}{'Mutation' if mutation else 'Query'}"


class _Group:
    """Tree node: routes ending at this exact path + child segments."""

    def __init__(self) -> None:
        self.own: list[RouteInfo] = []
        self.children: dict[str, _Group] = {}

    def insert(self, path: tuple[str, ...], route: RouteInfo) -> None:
        node = self
        for seg in path:
            node = node.children.setdefault(seg, _Group())
        node.own.append(route)

    def is_empty(self) -> bool:
        return not self.own and all(child.is_empty() for child in self.children.values())

    def prune(self) -> None:
        for seg in [s for s, c in self.children.items() if c.is_empty()]:
            del self.children[seg]
        for child in self.children.values():
            child.prune()


class SchemaBuilder:
    """Builds a domain-grouped ``GraphQLSchema`` from ``RouteInfo`` items."""

    def __init__(
        self,
        routes: Sequence[RouteInfo],
        invoker: RouteInvoker,
        types: TypeBuilder | None = None,
    ) -> None:
        self._routes = list(routes)
        self._invoker = invoker
        self._types = types or TypeBuilder()
        self._used_type_names: dict[str, tuple[str, ...]] = {}
        # Flat name -> field indexes consumed by the progressive-disclosure tools.
        self._query_fields: dict[str, GraphQLField] = {}
        self._mutation_fields: dict[str, GraphQLField] = {}

    @property
    def types(self) -> TypeBuilder:
        return self._types

    @property
    def query_fields(self) -> dict[str, GraphQLField]:
        return dict(self._query_fields)

    @property
    def mutation_fields(self) -> dict[str, GraphQLField]:
        return dict(self._mutation_fields)

    # ------------------------------------------------------------------ names

    def _unique_group_name(self, path: tuple[str, ...], *, mutation: bool) -> str:
        base = group_type_name(path, mutation=mutation)
        owner = self._used_type_names.get(base)
        if owner is not None and owner != path:
            candidate, counter = base, 2
            while self._used_type_names.get(candidate) not in (None, path):
                candidate = f"{base}{counter}"
                counter += 1
            logger.warning(
                "Group type name %r already owned by %s; using %r for %s",
                base, owner, candidate, path,
            )
            base = candidate
        self._used_type_names[base] = path
        return base

    # ----------------------------------------------------------------- build

    def _group_field(
        self, node: _Group, path: tuple[str, ...], *, mutation: bool
    ) -> GraphQLField:
        fields: dict[str, GraphQLField] = {}
        index = self._mutation_fields if mutation else self._query_fields
        for route in node.own:
            field = _leaf_field(route, self._invoker, self._types)
            index[route.field_name] = field
            fields[route.field_name] = field
        child_segments: list[str] = []
        for seg, child in sorted(node.children.items()):
            fields[seg] = self._group_field(child, (*path, seg), mutation=mutation)
            child_segments.append(seg)
        obj = GraphQLObjectType(
            name=self._unique_group_name(path, mutation=mutation),
            fields=fields,
        )

        # Group namespaces never fail: resolve to a dict pre-populated with the
        # child group keys so the default resolver chains one level deeper.
        def resolve_group(
            _root: Any, _info: Any, _keys: tuple[str, ...] = tuple(child_segments)
        ) -> Any:
            return {key: {} for key in _keys}

        return GraphQLField(GraphQLNonNull(obj), resolve=resolve_group)

    def _root_type(
        self, tree: _Group, *, mutation: bool
    ) -> GraphQLObjectType | None:
        tree.prune()
        if tree.is_empty():
            return None
        fields = {}
        for seg, child in sorted(tree.children.items()):
            fields[seg] = self._group_field(child, (seg,), mutation=mutation)
        if not fields:
            return None
        return GraphQLObjectType(
            name="Mutation" if mutation else "Query", fields=fields
        )

    def build(self) -> GraphQLSchema:
        query_routes = [r for r in self._routes if not r.is_mutation]
        mutation_routes = [r for r in self._routes if r.is_mutation]

        if not query_routes and not mutation_routes:
            raise RouterQLConfigError(
                "routerql found no routable endpoints: every route was skipped "
                "(see the routerql warning log for reasons)."
            )

        # Duplicate function-name check stays namespace-wide (Query / Mutation).
        validate_field_names([(r.field_name, r.method, r.path) for r in query_routes])
        if mutation_routes:
            validate_field_names(
                [(r.field_name, r.method, r.path) for r in mutation_routes]
            )

        query_tree = _Group()
        for route in query_routes:
            for domain in route.domains:
                query_tree.insert(domain, route)
        mutation_tree = _Group()
        for route in mutation_routes:
            for domain in route.domains:
                mutation_tree.insert(domain, route)

        query_type = self._root_type(query_tree, mutation=False)
        mutation_type = self._root_type(mutation_tree, mutation=True)
        if query_type is None and mutation_type is None:  # pragma: no cover
            raise RouterQLConfigError(
                "routerql found no routable endpoints: every route was skipped."
            )
        return GraphQLSchema(query=query_type, mutation=mutation_type)
