"""MCP progressive-disclosure tools for large apps.

Four layers over the tag-derived domain tree:

    list_domains -> list_queries(domain) -> get_query_schema(domain) -> graphql_query

Discovery is scoped per domain (a route's tags form its domain paths, the
``"a:b"`` separator adds depth); execution always runs against the FULL schema,
so agents may still combine fields across domains in one query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from graphql import GraphQLObjectType, GraphQLSchema, print_schema

from routerql.domains import DomainRegistry
from routerql.mcp.errors import (
    RouterQLErrors,
    create_error_response,
    create_success_response,
)
from routerql.mcp.tools import register_executor_tools

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from routerql.handler import RouterGraphQLHandler


def _parse_domain(domain: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in domain.split(":") if part.strip())


def _field_brief(name: str, field: Any) -> dict[str, Any]:
    args = [
        {"name": arg_name, "type": str(arg.type)}
        for arg_name, arg in (field.args or {}).items()
    ]
    return {
        "name": name,
        "type": str(field.type),
        "description": field.description,
        **({"args": args} if args else {}),
    }


def _domain_sdl(
    schema: GraphQLSchema, registry: DomainRegistry, path: tuple[str, ...]
) -> str | None:
    """SDL fragment containing the domain subtree's operations and types.

    Builds a filtered sub-schema from the selected root fields; graphql-core
    then auto-collects only the reachable types, so shared types referenced by
    the domain are included and everything else is left out.
    """
    queries, mutations = registry.subtree_fields(path)
    if not queries and not mutations:
        return None
    query_type = schema.query_type
    if query_type is None or not queries:
        return None
    sub_query = GraphQLObjectType(
        name=query_type.name,
        fields={name: query_type.fields[name] for name in queries},
    )
    sub_mutation = None
    if mutations and schema.mutation_type is not None:
        mutation_type = schema.mutation_type
        sub_mutation = GraphQLObjectType(
            name=mutation_type.name,
            fields={name: mutation_type.fields[name] for name in mutations},
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
        query_type = schema.query_type
        if query_type is None:  # pragma: no cover - schema always has Query
            return create_success_response({"domain": domain, "queries": []})
        names, _ = registry.subtree_fields(path)
        fields = [
            _field_brief(name, query_type.fields[name])
            for name in sorted(names)
            if name in query_type.fields
        ]
        return create_success_response(
            {"domain": domain, "queries": fields},
            hint="Call get_query_schema(domain) for the full SDL fragment of "
            "these operations, then graphql_query to execute.",
        )

    if allow_mutation:

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
            mutation_type = schema.mutation_type
            if mutation_type is None:
                return create_success_response({"domain": domain, "mutations": []})
            _, names = registry.subtree_fields(path)
            fields = [
                _field_brief(name, mutation_type.fields[name])
                for name in sorted(names)
                if name in mutation_type.fields
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
        sdl = _domain_sdl(schema, registry, path)
        if sdl is None:
            return create_error_response(
                f"Domain '{domain}' has no GraphQL operations.",
                RouterQLErrors.DOMAIN_NOT_FOUND,
                hint="Pick a domain from list_domains that carries operations.",
            )
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
        RouterQLErrors.DOMAIN_NOT_FOUND,
        hint=f"Available top-level domains: {', '.join(available)}. "
        f"Use list_domains to browse the full tree.",
    )
