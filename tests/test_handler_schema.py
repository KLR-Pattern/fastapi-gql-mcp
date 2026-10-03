"""schema_builder + handler: end-to-end GraphQL over FastAPI (no MCP yet)."""

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field

from fastapi_gql_mcp.handler import GQLMCPConfigError, RouterGraphQLHandler
from fastapi_gql_mcp.schema_builder import DuplicateArgError
from fastapi_gql_mcp.type_builder import TypeBuilder


class ItemOut(BaseModel):
    id: int
    name: str


class ItemCreate(BaseModel):
    name: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/items", response_model=list[ItemOut], tags=["shop"])
    async def list_items(limit: int = 2):
        return [ItemOut(id=i, name=f"i{i}") for i in range(limit)]

    @app.get("/items/{item_id}", response_model=ItemOut, tags=["shop"])
    async def get_item(item_id: int):
        return ItemOut(id=item_id, name="single")

    @app.post("/items", response_model=ItemOut, tags=["items"])
    async def create_item(payload: ItemCreate):
        return ItemOut(id=99, name=payload.name)

    return app


@pytest.fixture
def handler():
    h = RouterGraphQLHandler(build_app(), allow_mutation=True)
    yield h
    # lifespan may not have started if no query ran; aclose is idempotent-safe


class TestSDLAndIntrospection:
    def test_sdl_contains_types_and_fields(self, handler):
        sdl = handler.get_sdl()
        assert "type Query {" in sdl
        assert "shop: ShopQuery!" in sdl
        assert "list_items(limit: Int = 2): [ItemOut!]" in sdl
        assert "get_item(item_id: Int!): ItemOut" in sdl
        assert "type Mutation {" in sdl
        assert "items: ItemsMutation!" in sdl
        assert "create_item(payload: ItemCreateInput!): ItemOut" in sdl
        assert "type ItemOut {" in sdl
        assert "input ItemCreateInput {" in sdl

    def test_mutation_gated_off(self):
        h = RouterGraphQLHandler(build_app(), allow_mutation=False)
        assert "Mutation" not in h.get_sdl()
        assert "create_item" not in h.get_sdl()

    async def test_introspection_works(self, handler):
        result = await handler.execute("{ __schema { queryType { name } } }")
        assert result == {"data": {"__schema": {"queryType": {"name": "Query"}}}}


class TestExecution:
    async def test_query_flat(self, handler):
        result = await handler.execute("{ shop { list_items { id name } } }")
        assert result == {
            "data": {"shop": {"list_items": [{"id": 0, "name": "i0"}, {"id": 1, "name": "i1"}]}}
        }

    async def test_field_projection(self, handler):
        result = await handler.execute("{ shop { list_items { name } } }")
        assert result == {"data": {"shop": {"list_items": [{"name": "i0"}, {"name": "i1"}]}}}

    async def test_query_with_args(self, handler):
        result = await handler.execute("{ shop { list_items(limit: 1) { id } } }")
        assert result == {"data": {"shop": {"list_items": [{"id": 0}]}}}

    async def test_path_params(self, handler):
        result = await handler.execute("{ shop { get_item(item_id: 5) { id name } } }")
        assert result == {"data": {"shop": {"get_item": {"id": 5, "name": "single"}}}}

    async def test_variables(self, handler):
        result = await handler.execute(
            "query($id: Int!) { shop { get_item(item_id: $id) { name } } }",
            variables={"id": 3},
        )
        assert result == {"data": {"shop": {"get_item": {"name": "single"}}}}

    async def test_variable_defaults(self, handler):
        result = await handler.execute(
            "query($limit: Int = 1) { shop { list_items(limit: $limit) { id } } }"
        )
        assert result == {"data": {"shop": {"list_items": [{"id": 0}]}}}

    async def test_alias_and_multiple_fields(self, handler):
        result = await handler.execute(
            "{ shop { a: list_items(limit: 1) { id } b: get_item(item_id: 7) { id } } }"
        )
        assert result["data"]["shop"]["a"] == [{"id": 0}]
        assert result["data"]["shop"]["b"] == {"id": 7}

    async def test_mutation_execution(self, handler):
        result = await handler.execute(
            'mutation { items { create_item(payload: {name: "n"}) { id name } } }'
        )
        assert result == {"data": {"items": {"create_item": {"id": 99, "name": "n"}}}}

    async def test_validation_error_format(self, handler):
        result = await handler.execute("{ nope }")
        assert "data" not in result
        assert "Cannot query field 'nope'" in result["errors"][0]["message"]

    async def test_resolver_error_format(self):
        from fastapi import HTTPException

        app = FastAPI()

        @app.get("/missing/{item_id}", response_model=ItemOut, tags=["misc"])
        async def missing(item_id: int):
            raise HTTPException(status_code=404, detail="not found")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ misc { missing(item_id: 1) { id } } }")
        assert result["data"] == {"misc": {"missing": None}}
        error = result["errors"][0]
        assert error["extensions"]["code"] == "HTTP_404"
        assert "404" in error["message"]
        await handler.aclose()


class TestConfigErrors:
    def test_no_routable_routes(self):
        app = FastAPI()

        @app.get("/ping")
        async def ping():
            return {}

        with pytest.raises(GQLMCPConfigError, match="no routable endpoints"):
            RouterGraphQLHandler(app)

    def test_duplicate_arg_names(self):
        from fastapi_gql_mcp.scanner import ParamInfo, RouteInfo
        from fastapi_gql_mcp.schema_builder import _arguments

        route = RouteInfo(
            route=None,  # type: ignore[arg-type]
            method="GET",
            path="/x",
            field_name="get_x",
            path_params=(ParamInfo("id", int, True),),
            query_params=(ParamInfo("id", str, False, "v"),),
        )
        with pytest.raises(DuplicateArgError, match="argument 'id'"):
            _arguments(route, TypeBuilder())


class TestDuplicateEndpointNames:
    def test_same_function_name_on_two_routes_fails_fast(self):
        app = FastAPI()

        @app.get("/a/{a_id}", response_model=ItemOut)
        async def get_thing(a_id: int):
            return ItemOut(id=a_id, name="a")

        @app.get("/b/{b_id}", response_model=ItemOut)
        async def get_thing(b_id: int):  # noqa: F811 — the collision under test
            return ItemOut(id=b_id, name="b")

        from fastapi_gql_mcp.naming import DuplicateFieldError

        with pytest.raises(DuplicateFieldError, match="Rename one endpoint function"):
            RouterGraphQLHandler(app)

    def test_same_name_across_namespaces_is_fine(self):
        app = FastAPI()

        @app.get("/things/{thing_id}", response_model=ItemOut)
        async def thing(thing_id: int):
            return ItemOut(id=thing_id, name="q")

        @app.post("/things", response_model=ItemOut)
        async def thing(payload: ItemCreate):  # noqa: F811
            return ItemOut(id=1, name=payload.name)

        handler = RouterGraphQLHandler(app, allow_mutation=True)
        assert "thing" in handler.get_sdl()


class TestDescriptions:
    def _sdl(self):
        from typing import Annotated

        from fastapi import Body, Query

        class Documented(BaseModel):
            """A documented response model."""

            id: int
            note: str = Field(description="field-level description")

        app = FastAPI()

        @app.get("/search", response_model=list[Documented], tags=["docs"])
        async def search(
            q: Annotated[str, Query(description="full-text search terms")] = "",
            limit: Annotated[int, Query(ge=1)] = 5,
        ) -> list[Documented]:
            """Search documented things.

            Docstring body with details.
            """
            return [Documented(id=1, note="x")]

        @app.post("/things", response_model=Documented, tags=["docs"])
        async def create(
            payload: Annotated[Documented, Body(description="the thing to create")],
        ) -> Documented:
            return payload

        return RouterGraphQLHandler(app, allow_mutation=True).get_sdl()

    def test_model_docstring_becomes_type_description(self):
        sdl = self._sdl()
        assert '"""\nA documented response model.\n"""' in sdl or (
            '"""A documented response model."""' in sdl
        )

    def test_field_description_present(self):
        assert '"""field-level description"""' in self._sdl()

    def test_endpoint_docstring_becomes_field_description(self):
        sdl = self._sdl()
        assert "Search documented things." in sdl
        assert "Docstring body with details." in sdl

    def test_param_description_becomes_argument_description(self):
        sdl = self._sdl()
        assert '"""full-text search terms"""' in sdl
        assert '"""the thing to create"""' in sdl

    def test_undocumented_params_have_no_description(self):
        sdl = self._sdl()
        # limit has no Query(description=...): its SDL line is bare
        assert "limit: Int = 5" in sdl
