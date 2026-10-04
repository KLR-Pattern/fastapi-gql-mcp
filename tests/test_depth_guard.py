"""P0-1 depth guard: runaway nesting is rejected before execution.

Recursive models make query depth unbounded and an MCP caller is an LLM
that can emit pathological documents — the guard must reject them at the
handler's choke point, including depth hidden behind fragment spreads.
"""

import pytest
from fastapi import FastAPI
from graphql import parse
from pydantic import BaseModel

from fastapi_gql_mcp.depth_guard import _FragmentCycle, depth_error, document_depth
from fastapi_gql_mcp.handler import RouterGraphQLHandler


class Node(BaseModel):
    value: int
    children: list["Node"] = []


Node.model_rebuild()


def node_app() -> FastAPI:
    app = FastAPI()

    @app.get("/node/{node_id}", response_model=Node, tags=["tree"])
    async def get_node(node_id: int) -> Node:
        return Node(value=node_id)

    return app


def nested_query(levels: int) -> str:
    """``{ tree { get_node(node_id: 1) { children {…} value } } }`` — depth = levels + 3."""
    inner = "value"
    for _ in range(levels):
        inner = f"children {{ {inner} }}"
    return f"{{ tree {{ get_node(node_id: 1) {{ {inner} }} }} }}"


def fragment_query(body_levels: int) -> str:
    """Shallow on paper (depth 3); the fragment carries the real nesting."""
    body = "value"
    for _ in range(body_levels):
        body = f"children {{ {body} }}"
    return (
        "query { tree { get_node(node_id: 1) { ...Deep } } } "
        f"fragment Deep on Node {{ {body} }}"
    )


class TestDocumentDepth:
    def test_depth_semantics(self):
        assert document_depth(parse("{ a }")) == 1
        assert document_depth(parse("{ a { b } }")) == 2
        assert document_depth(parse("{ a { b { c { d } } } }")) == 4

    def test_fragment_resolves_without_extra_level(self):
        doc = "query { a { ...F } } fragment F on T { b { c } }"
        assert document_depth(parse(doc)) == 3  # same as inlined { a { b { c } } }

    def test_inline_fragment_is_transparent(self):
        assert document_depth(parse("{ a { ... on T { b } } }")) == 2

    def test_cyclic_fragment_raises(self):
        doc = "query { ...F } fragment F on T { a { ...F } }"
        with pytest.raises(_FragmentCycle):
            document_depth(parse(doc))


class TestDepthError:
    def test_within_bounds_returns_none(self):
        assert depth_error("{ a { b { c } } }", 10) is None

    def test_too_deep_returns_error(self):
        error = depth_error("{ a { b { c { d { e { f } } } } } }", 5)
        assert error is not None
        assert "exceeds max_depth=5" in error.message

    def test_syntax_error_surfaces(self):
        assert depth_error("{ a {", 10) is not None  # same envelope as graphql()


class TestHandlerIntegration:
    async def test_deep_query_rejected_before_execution(self):
        handler = RouterGraphQLHandler(node_app())  # default max_depth=10
        result = await handler.execute(nested_query(20))  # depth 23
        assert "data" not in result
        assert "exceeds max_depth=10" in result["errors"][0]["message"]
        await handler.aclose()

    async def test_reasonable_depth_passes(self):
        handler = RouterGraphQLHandler(node_app())  # depth 8 < 10
        result = await handler.execute(nested_query(5))
        assert result["data"]["tree"]["get_node"]["children"] == []
        await handler.aclose()

    async def test_fragment_hidden_depth_rejected(self):
        handler = RouterGraphQLHandler(node_app())
        result = await handler.execute(fragment_query(12))  # real depth 15 > 10
        assert "data" not in result
        assert "exceeds max_depth=10" in result["errors"][0]["message"]
        await handler.aclose()

    async def test_cyclic_fragments_rejected(self):
        handler = RouterGraphQLHandler(node_app())
        query = "query { tree { get_node(node_id: 1) { ...F } } } " \
                "fragment F on Node { children { ...F } }"
        result = await handler.execute(query)
        assert "cyclic fragment" in result["errors"][0]["message"]
        await handler.aclose()

    async def test_max_depth_none_disables_guard(self):
        handler = RouterGraphQLHandler(node_app(), max_depth=None)
        result = await handler.execute(nested_query(20))
        assert result["data"]["tree"]["get_node"]["children"] == []
        await handler.aclose()

    async def test_custom_max_depth(self):
        handler = RouterGraphQLHandler(node_app(), max_depth=3)
        result = await handler.execute(nested_query(5))  # depth 8 > 3
        assert "exceeds max_depth=3" in result["errors"][0]["message"]
        await handler.aclose()

    def test_invalid_max_depth_rejected(self):
        with pytest.raises(ValueError, match="max_depth"):
            RouterGraphQLHandler(node_app(), max_depth=0)


class TestIntrospectionExempt:
    """GraphiQL/codegen ship a fixed deep introspection document (depth
    ~15) — meta-fields must not count toward max_depth, or every standard
    tool breaks out of the box while the data guard stays intact."""

    def test_standard_introspection_query_passes_default_guard(self):
        from graphql.utilities import get_introspection_query

        assert depth_error(get_introspection_query(), max_depth=10) is None

    def test_deep_data_selection_still_rejected(self):
        query = (
            "query Deep { t { a { b { c { d { e { f { g { h { i { j { k } } } } } } } } } } } }"
        )
        err = depth_error(query, max_depth=10)
        assert err is not None and "exceeds max_depth=10" in err.message

    async def test_graphiql_schema_fetch_against_handler(self):
        from graphql.utilities import get_introspection_query

        handler = RouterGraphQLHandler(node_app())  # default max_depth=10
        result = await handler.execute(get_introspection_query())
        assert "errors" not in result, result.get("errors")
        await handler.aclose()
