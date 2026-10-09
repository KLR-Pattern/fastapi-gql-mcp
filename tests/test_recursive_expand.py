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
        """The template is the STOPPING level's selection, applied to every
        descendant. A rich model (extra fields the query never selects below
        the top) makes a filtering regression observable: if unrolling lost
        the template, description/price/sku would leak into descendants."""

        class RichNode(BaseModel):
            name: str
            description: str = "must not leak into descendants"
            price_cents: int = 999
            sku: str = "SKU-X"
            children: list["RichNode"] = []

        RichNode.model_rebuild()

        def rich_chain(depth: int, name: str) -> RichNode:
            if depth == 1:
                return RichNode(name=name)
            return RichNode(
                name=name, children=[rich_chain(depth - 1, f"{name}.{depth}")]
            )

        app = FastAPI()

        @app.get("/rich", response_model=RichNode, tags=["t"])
        async def rich() -> RichNode:
            return rich_chain(5, "root")

        handler = RouterGraphQLHandler(app)
        # top level selects {name, description}; the STOPPING level (inside
        # children) selects only {name}: descendants must carry exactly that.
        result = await handler.execute(
            "{ t { rich { name description children { name } } } }"
        )
        assert "errors" not in result, result
        root = result["data"]["t"]["rich"]
        assert set(root.keys()) == {"name", "description", "children"}

        def walk_descendants(node: dict) -> None:
            for child in node["children"]:
                assert set(child.keys()) == {"name", "children"}, sorted(child.keys())
                walk_descendants(child)

        walk_descendants(root)
        assert deepest(root) == 5  # filtering AND full depth together

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


class TestUnrollLimit:
    async def test_limit_tracks_recursion_budget(self, monkeypatch):
        from fastapi_gql_mcp.recursive_expand import unroll_limit

        monkeypatch.setattr("sys.setrecursionlimit", lambda n: None)
        monkeypatch.setattr("sys.getrecursionlimit", lambda: 1000)
        assert unroll_limit() == 100  # default budget → floor of 100

        monkeypatch.setattr("sys.getrecursionlimit", lambda: 20000)
        assert unroll_limit() == 2000  # raised budget scales up with it

    async def test_floor_hit_reports_error_not_silent(self, monkeypatch):
        """Data deeper than the scaffold must never vanish silently: the
        errors channel carries a notice while the data stays."""
        TREE = {"name": "n", "children": [
            {"name": "n", "children": [
                {"name": "n", "children": [
                    {"name": "n", "children": [
                        {"name": "n", "children": []},
                    ]},
                ]},
            ]},
        ]}  # 5 levels against a scaffold of 3

        monkeypatch.setattr("fastapi_gql_mcp.handler.unroll_limit", lambda: 3)
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            raise RuntimeError("invoker patched")

        handler = RouterGraphQLHandler(app)

        async def fake_invoke(*a, **k):
            return TREE

        handler._invoker.invoke = fake_invoke
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert result["data"]["t"]["tree"] is not None  # data stays
        messages = [e["message"] for e in result.get("errors", [])]
        assert any("unroll limit (3 levels)" in m for m in messages), messages
        assert any("may have been truncated" in m for m in messages)

    async def test_no_floor_error_below_limit(self, monkeypatch):
        monkeypatch.setattr("fastapi_gql_mcp.handler.unroll_limit", lambda: 10)
        app = FastAPI()

        @app.get("/tree", response_model=Node, tags=["t"])
        async def tree():
            return chain(3, "root")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ t { tree { name children { name } } } }")
        assert "errors" not in result, result

    async def test_raised_budget_serves_deep_tree(self):
        """The adaptive limit in action: raise the process budget and a
        200-level tree serves in full, no floor error."""
        import sys

        old = sys.getrecursionlimit()
        sys.setrecursionlimit(10000)
        try:
            TREE = {"name": "n", "children": []}
            node = TREE
            for _ in range(199):
                node["children"] = [{"name": "n", "children": []}]
                node = node["children"][0]

            app = FastAPI()

            @app.get("/tree", response_model=Node, tags=["t"])
            async def tree():
                raise RuntimeError("invoker patched")

            handler = RouterGraphQLHandler(app)

            async def fake_invoke(*a, **k):
                return TREE

            handler._invoker.invoke = fake_invoke
            result = await handler.execute(
                "{ t { tree { name children { name } } } }"
            )
            assert "errors" not in result, result
            assert deepest(result["data"]["t"]["tree"]) == 200
        finally:
            sys.setrecursionlimit(old)
