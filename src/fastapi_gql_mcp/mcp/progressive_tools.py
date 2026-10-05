"""MCP progressive-disclosure tools for large apps.

Four layers over the tag-derived domain tree:

    list_domains -> list_queries(domain) -> get_query_schema(domain) -> graphql_query

Discovery is scoped per domain (a route's tags form its domain paths, the
``"a:b"`` separator adds depth); execution always runs against the FULL schema,
so agents may still combine fields across domains in one query.

The schema itself mirrors the same hierarchy: fields live under their domain
groups (``{ shop { catalog { list_products } } }``), so the fragment an agent
reads matches the query it writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from graphql import (
    GraphQLField,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    print_schema,
)

from fastapi_gql_mcp.domains import DomainRegistry
from fastapi_gql_mcp.mcp.errors import (
    GQLMCPErrors,
    create_error_response,
    create_success_response,
)
from fastapi_gql_mcp.mcp.tools import register_executor_tools

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from fastapi_gql_mcp.handler import RouterGraphQLHandler


def _parse_domain(domain: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in domain.split(":") if part.strip())


def _field_brief(name: str, field: Any) -> dict[str, Any]:
    args = [
        {
            "name": arg_name,
            "type": str(arg.type),
            "description": arg.description,
        }
        for arg_name, arg in (field.args or {}).items()
    ]
    return {
        "name": name,
        "type": str(field.type),
        "description": field.description,
        **({"deprecated": True} if field.deprecation_reason is not None else {}),
        **({"args": args} if args else {}),
    }


def _unwrap(obj: Any) -> Any:
    return obj.of_type if isinstance(obj, GraphQLNonNull) else obj


def _walk_group(root_type: Any, path: tuple[str, ...]) -> GraphQLObjectType | None:
    """The group object type sitting at ``path`` under a root operation type."""
    current: Any = root_type
    for seg in path:
        if current is None:
            return None
        field = current.fields.get(seg)
        if field is None:
            return None
        current = _unwrap(field.type)
    return current if isinstance(current, GraphQLObjectType) else None


def _domain_sdl(
    schema: GraphQLSchema, registry: DomainRegistry, path: tuple[str, ...]
) -> str | None:
    """SDL fragment for one domain subtree, addressed exactly as in queries.

    Re-wraps the main schema's group object for ``path`` up the ancestor
    chain, so the fragment shows the same route from the Query root
    (``Query { shop { catalog { ... } } }``) while graphql-core auto-collects
    only the reachable types.
    """
    from fastapi_gql_mcp.schema_builder import group_type_name

    queries, mutations = registry.subtree_fields(path)
    if not queries and not mutations:
        return None

    def wrap(
        root_type: Any, path: tuple[str, ...], *, mutation: bool
    ) -> GraphQLObjectType | None:
        group = _walk_group(root_type, path)
        if group is None:
            return None
        # Rebuild the ancestor chain with sibling-free wrappers that keep the
        # real group names, so the fragment's field addresses match the schema.
        current: GraphQLObjectType = group
        for depth in range(len(path) - 1, 0, -1):
            ancestors = path[:depth]
            child_seg = path[depth]
            wrapper = GraphQLObjectType(
                name=group_type_name(ancestors, mutation=mutation),
                fields={child_seg: GraphQLField(GraphQLNonNull(current))},
            )
            current = wrapper
        return current

    sub_query_obj = wrap(schema.query_type, path, mutation=False)
    if sub_query_obj is None:
        return None
    sub_query = GraphQLObjectType(
        name="Query", fields={path[0]: GraphQLField(GraphQLNonNull(sub_query_obj))}
    )
    sub_mutation = None
    if mutations and schema.mutation_type is not None:
        sub_mutation_obj = wrap(schema.mutation_type, path, mutation=True)
        if sub_mutation_obj is not None:
            sub_mutation = GraphQLObjectType(
                name="Mutation",
                fields={path[0]: GraphQLField(GraphQLNonNull(sub_mutation_obj))},
            )
    return print_schema(GraphQLSchema(query=sub_query, mutation=sub_mutation))


def register_progressive_tools(
    mcp: FastMCP,
    handler: RouterGraphQLHandler,
    registry: DomainRegistry,
    *,
    allow_mutation: bool,
) -> None:
    """Register the 4-layer progressive disclosure toolset on a FastMCP."""

    schema = handler.schema

    def _resolve(domain: str) -> tuple[str, ...] | None:
        path = _parse_domain(domain)
        if registry.node(path) is None:
            return None
        return path

    @mcp.tool()
    def list_domains() -> dict[str, Any]:
        """List the API's domains (derived from route tags) with operation counts.

        Domains form a tree: a tag ``"billing:invoice"`` yields the domain
        ``billing`` with subdomain ``invoice``. Untagged routes are grouped by
        their first path segment. Start here, then drill in with
        ``list_queries(domain)`` using a domain path like ``"billing:invoice"``.

        Returns:
            dict with success/data: list of
            {name, path, queries, mutations, subdomains?} plus a hint.
        """
        summary = registry.summary()
        return create_success_response(
            {"domains": summary},
            hint="Call list_queries(domain) with one of these domain paths.",
        )

    index = handler.query_fields
    # The schema is immutable after build, so each domain's SDL fragment is
    # computed once and reused — agents re-explore the same domain often.
    sdl_cache: dict[tuple[str, ...], str] = {}

    @mcp.tool()
    def list_queries(domain: str) -> dict[str, Any]:
        """List the read (Query) operations of one domain.

        Args:
            domain: Domain path from list_domains, e.g. ``"billing:invoice"``.
                Covers the whole subtree (``"billing"`` includes its
                subdomains).

        Returns:
            dict with success/data: list of {name, type, args?, description?}
            for each Query field, plus a hint towards get_query_schema.
        """
        path = _resolve(domain)
        if path is None:
            return _unknown_domain(domain, registry)
        names, _ = registry.subtree_fields(path)
        fields = [
            _field_brief(name, index[(owner, name)])
            for owner, name in sorted(names)
            if (owner, name) in index
        ]
        return create_success_response(
            {"domain": domain, "queries": fields},
            hint="Call get_query_schema(domain) for the full SDL fragment of "
            "these operations, then graphql_query to execute.",
        )

    if allow_mutation:

        mutation_index = handler.mutation_fields

        @mcp.tool()
        def list_mutations(domain: str) -> dict[str, Any]:
            """List the write (Mutation) operations of one domain.

            Args:
                domain: Domain path from list_domains, e.g. ``"billing"``.

            Returns:
                dict with success/data: list of {name, type, args?,
                description?} for each Mutation field.
            """
            path = _resolve(domain)
            if path is None:
                return _unknown_domain(domain, registry)
            _, names = registry.subtree_fields(path)
            fields = [
                _field_brief(name, mutation_index[(owner, name)])
                for owner, name in sorted(names)
                if (owner, name) in mutation_index
            ]
            return create_success_response({"domain": domain, "mutations": fields})

    @mcp.tool()
    def get_query_schema(domain: str) -> dict[str, Any]:
        """Get the SDL fragment for one domain: its operations and reachable types.

        Args:
            domain: Domain path from list_domains, e.g. ``"billing:invoice"``.

        The fragment contains the domain's Query fields (and Mutation fields
        when present) plus every type they reach — nothing else. Execution is
        NOT restricted to the domain: graphql_query runs against the full
        schema, so fields from different domains may be combined.

        Returns:
            dict with success/data: {"sdl": "<SDL fragment>"} and a hint.
        """
        path = _resolve(domain)
        if path is None:
            return _unknown_domain(domain, registry)
        sdl = sdl_cache.get(path)
        if sdl is None:
            sdl = _domain_sdl(schema, registry, path)
            if sdl is None:
                return create_error_response(
                    f"Domain '{domain}' has no GraphQL operations.",
                    GQLMCPErrors.DOMAIN_NOT_FOUND,
                    hint="Pick a domain from list_domains that carries operations.",
                )
            sdl_cache[path] = sdl
        return create_success_response(
            {"sdl": sdl},
            hint="Write a GraphQL query against this fragment and run it with "
            "graphql_query (other domains' fields may be combined too).",
        )

    register_executor_tools(mcp, handler, allow_mutation=allow_mutation)


def _unknown_domain(domain: str, registry: DomainRegistry) -> dict[str, Any]:
    available = [
        ":".join(str(part) for part in cast("list[str]", s["path"]))
        for s in registry.summary()
    ]
    return create_error_response(
        f"Unknown domain '{domain}'.",
        GQLMCPErrors.DOMAIN_NOT_FOUND,
        hint=f"Available top-level domains: {', '.join(available)}. "
        f"Use list_domains to browse the full tree.",
    )
