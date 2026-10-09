"""Recursive-chain unrolling: recursive fields return their true depth.

The route already computed the full tree; unrolling removes the document's
depth limitation. Selecting a recursive field means "the whole subtree" —
the selection shape where the agent stops repeats to whatever depth the
data has, at any position in the document.
"""

from fastapi import FastAPI
from pydantic import BaseModel

from fastapi_gql_mcp.handler import RouterGraphQLHandler


class Node(BaseModel):
    name: str
    children: list["Node"] = []


Node.model_rebuild()


def chain(depth: int, name: str) -> Node:
    if depth == 1:
        return Node(name=name, children=[])
    return Node(name=name, children=[chain(depth - 1, f"{name}.{depth}")])


def deepest(node: dict) -> int:
    return 1 + (deepest(node["children"][0]) if node.get("children") else 0)


class TestTrueDepth:
    async def test_shallow_selection_returns_full_depth(self):
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(6, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 6

    async def test_field_filtering_repeats_per_level(self):
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(6, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { tree { name children { name } } } }")
        node = result["data"]["t"]["tree"]
        for _ in range(6):
            assert set(node.keys()) == {"name", "children"}  # no extra fields
            node = node["children"][0] if node["children"] else node

    async def test_manual_deeper_selection_still_complete(self):
        """Hand-writing more levels than needed is legal GraphQL; the chain
        end still stamps the remainder, so the result is the same tree."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(5, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { children { children { name } } } } }"
        )
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["tree"]) == 5

    async def test_chain_mid_document_siblings_untouched(self):
        """The recursive type enters below variants.parent — the chain is
        detected at any position and only the chain itself deepens."""
        class Variant(BaseModel):
            sku: str
            parent: Node

        class Product(BaseModel):
            title: str
            variants: list[Variant]

        app = FastAPI()

        @app.get("/products", response_model=list[Product], tags=["t"])
        async def list_products():
            return [
                Product(title="p1", variants=[Variant(sku="v1", parent=chain(6, "n"))])
            ]

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { list_products { title variants { sku parent { name"
            " children { name } } } } } }"
        )
        assert "errors" not in result, result
        product = result["data"]["t"]["list_products"][0]
        assert product["title"] == "p1" and product["variants"][0]["sku"] == "v1"
        assert deepest(product["variants"][0]["parent"]) == 6

    async def test_multiple_chains_independent(self):
        app = FastAPI()

        @app.get("/a", response_model=Node, tags=["t"])
        async def a():
            return chain(3, "a")

        @app.get("/b", response_model=Node, tags=["t"])
        async def b():
            return chain(5, "b")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { a { name children { name } } b { name children { name } } } }"
        )
        assert "errors" not in result, result
        assert deepest(result["data"]["t"]["a"]) == 3
        assert deepest(result["data"]["t"]["b"]) == 5


class TestBoundaries:
    async def test_max_depth_still_guards_the_written_document(self):
        """The runaway guard sees the AGENT's document; unrolling happens
        after it and is a system behavior, not an injection bypass."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(2, "root")

        handler = RouterGraphQLHandler(app, max_depth=5)
        query = (
            "{ t { tree { children { children { children { name } } } } } }"
        )
        result = await handler.execute(query)
        assert "data" not in result
        assert "exceeds max_depth=5" in result["errors"][0]["message"]

    async def test_fragment_in_template_left_unexpanded(self):
        """Fragments cannot express recursion (spec forbids the cycle), so a
        template carrying one degrades honestly to the written document —
        selected depth only."""
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(4, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ t { tree { name children { ... on Node { name } } } } }"
        )
        assert "errors" not in result, result
        # unexpanded: the selected depth is what comes back
        node = result["data"]["t"]["tree"]["children"][0]
        assert set(node.keys()) == {"name"}

    async def test_mutual_recursion_untouched(self):
        """A.b: B / B.a: A is a cycle without a direct self-edge — not
        unrolled (v1 boundary), and building/querying still works."""
        class B(BaseModel):
            tag: str
            a: "A | None" = None

        class A(BaseModel):
            name: str
            b: B | None = None

        A.model_rebuild()
        B.model_rebuild()

        app = FastAPI()

        @app.get("/pair", response_model=A, tags=["t"])
        async def pair():
            return A(name="x", b=B(tag="y"))

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { pair { name b { tag } } } }")
        assert "errors" not in result, result
        assert result["data"]["t"]["pair"]["b"] == {"tag": "y"}


class TestSchemaNote:
    async def test_contract_note_once_per_type_not_per_field(self):
        """The true-depth contract rides the recursive TYPE's description —
        one copy however many self-referencing fields the type carries (the
        SDL is token-priced; per-field copies would be redundant), placed
        where agents read the field list they select into."""
        class Family(BaseModel):  # two self-referencing fields on one type
            name: str
            siblings: list["Family"] = []
            cousins: list["Family"] = []

        Family.model_rebuild()
        app = FastAPI()

        @app.get("/family", response_model=Family, tags=["t"])
        async def family():
            return Family(name="x", siblings=[], cousins=[])

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        assert sdl.count("full subtree at true depth") == 1  # once per type
        assert sdl.count("repeats per level") == 1
        assert "siblings: [Family!]" in sdl and "cousins: [Family!]" in sdl
