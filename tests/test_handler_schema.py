"""schema_builder + handler: end-to-end GraphQL over FastAPI (no MCP yet)."""

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from routerql.handler import RouterGraphQLHandler, RouterQLConfigError
from routerql.schema_builder import DuplicateArgError
from routerql.type_builder import TypeBuilder


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

    @app.post("/items", response_model=ItemOut)
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
        assert "get_items(limit: Int = 2): [ItemOut!]" in sdl
        assert "get_items_by_item_id(item_id: Int!): ItemOut" in sdl
        assert "type Mutation {" in sdl
        assert "create_items(payload: ItemCreateInput!): ItemOut" in sdl
        assert "type ItemOut {" in sdl
        assert "input ItemCreateInput {" in sdl

    def test_mutation_gated_off(self):
        h = RouterGraphQLHandler(build_app(), allow_mutation=False)
        assert "Mutation" not in h.get_sdl()
        assert "create_items" not in h.get_sdl()

    async def test_introspection_works(self, handler):
        result = await handler.execute("{ __schema { queryType { name } } }")
        assert result == {"data": {"__schema": {"queryType": {"name": "Query"}}}}


class TestExecution:
    async def test_query_flat(self, handler):
        result = await handler.execute("{ get_items { id name } }")
        assert result == {
            "data": {"get_items": [{"id": 0, "name": "i0"}, {"id": 1, "name": "i1"}]}
        }

    async def test_field_projection(self, handler):
        result = await handler.execute("{ get_items { name } }")
        assert result == {"data": {"get_items": [{"name": "i0"}, {"name": "i1"}]}}

    async def test_query_with_args(self, handler):
        result = await handler.execute("{ get_items(limit: 1) { id } }")
        assert result == {"data": {"get_items": [{"id": 0}]}}

    async def test_path_params(self, handler):
        result = await handler.execute("{ get_items_by_item_id(item_id: 5) { id name } }")
        assert result == {"data": {"get_items_by_item_id": {"id": 5, "name": "single"}}}

    async def test_variables(self, handler):
        result = await handler.execute(
            "query($id: Int!) { get_items_by_item_id(item_id: $id) { name } }",
            variables={"id": 3},
        )
        assert result == {"data": {"get_items_by_item_id": {"name": "single"}}}

    async def test_variable_defaults(self, handler):
        result = await handler.execute(
            "query($limit: Int = 1) { get_items(limit: $limit) { id } }"
        )
        assert result == {"data": {"get_items": [{"id": 0}]}}

    async def test_alias_and_multiple_fields(self, handler):
        result = await handler.execute(
            "{ a: get_items(limit: 1) { id } b: get_items_by_item_id(item_id: 7) { id } }"
        )
        assert result["data"]["a"] == [{"id": 0}]
        assert result["data"]["b"] == {"id": 7}

    async def test_mutation_execution(self, handler):
        result = await handler.execute(
            'mutation { create_items(payload: {name: "n"}) { id name } }'
        )
        assert result == {"data": {"create_items": {"id": 99, "name": "n"}}}

    async def test_validation_error_format(self, handler):
        result = await handler.execute("{ nope }")
        assert "data" not in result
        assert "Cannot query field 'nope'" in result["errors"][0]["message"]

    async def test_resolver_error_format(self):
        from fastapi import HTTPException

        app = FastAPI()

        @app.get("/missing/{item_id}", response_model=ItemOut)
        async def missing(item_id: int):
            raise HTTPException(status_code=404, detail="not found")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ get_missing_by_item_id(item_id: 1) { id } }")
        assert result["data"] == {"get_missing_by_item_id": None}
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

        with pytest.raises(RouterQLConfigError, match="no routable endpoints"):
            RouterGraphQLHandler(app)

    def test_duplicate_arg_names(self):
        from routerql.scanner import ParamInfo, RouteInfo
        from routerql.schema_builder import _arguments

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
