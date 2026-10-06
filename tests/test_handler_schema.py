"""schema_builder + handler: end-to-end GraphQL over FastAPI (no MCP yet)."""

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field

from fastapi_gql_mcp.handler import GQLMCPConfigError, RouterGraphQLHandler
from fastapi_gql_mcp.scanner import RouterScanner
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


class TestDomainCollisions:
    """A leaf field and a child domain segment landing in the SAME group
    object type compete for one GraphQL field name — this must fail fast
    (same policy as two same-named leaves), never silently drop the leaf."""

    def test_leaf_colliding_with_child_domain_fails_fast(self):
        from fastapi_gql_mcp.naming import DuplicateFieldError

        class Summary(BaseModel):
            total: int

        class Product(BaseModel):
            id: int

        app = FastAPI()

        @app.get("/catalog-summary", response_model=Summary, tags=["shop"])
        async def catalog() -> Summary:
            """Leaf field 'catalog' inside ShopQuery..."""
            return Summary(total=5)

        @app.get("/products", response_model=list[Product], tags=["shop:catalog"])
        async def list_products() -> list[Product]:
            """...and the child domain segment 'catalog' also inside ShopQuery."""
            return [Product(id=1)]

        with pytest.raises(DuplicateFieldError):
            RouterGraphQLHandler(app)

    async def test_leaf_coexisting_with_child_domain_is_kept(self):
        """Non-colliding leaf + child group in one domain must both survive."""

        class Summary(BaseModel):
            total: int

        class Product(BaseModel):
            id: int

        app = FastAPI()

        @app.get("/catalog-summary", response_model=Summary, tags=["shop"])
        async def stats() -> Summary:
            return Summary(total=5)

        @app.get("/products", response_model=list[Product], tags=["shop:catalog"])
        async def list_products() -> list[Product]:
            return [Product(id=1)]

        h = RouterGraphQLHandler(app)
        sdl = h.get_sdl()
        assert "stats: Summary" in sdl
        assert "catalog: ShopCatalogQuery" in sdl
        result = await h.execute("{ shop { stats { total } } }")
        assert result == {"data": {"shop": {"stats": {"total": 5}}}}


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
        from typing import Annotated

        from fastapi import Form

        app = FastAPI()

        @app.post("/upload")
        async def upload(name: Annotated[str, Form()]) -> dict:
            return {"name": name}

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
    def test_same_name_same_domain_fails_fast(self):
        app = FastAPI()

        @app.get("/users/{user_id}", response_model=ItemOut, tags=["iam"])
        async def get_user(user_id: int):
            return ItemOut(id=user_id, name="a")

        @app.get("/staff/users/{user_id}", response_model=ItemOut, tags=["iam"])
        async def get_user(user_id: int):  # noqa: F811 — the collision under test
            return ItemOut(id=user_id, name="b")

        from fastapi_gql_mcp.naming import DuplicateFieldError

        with pytest.raises(DuplicateFieldError, match="domain group 'iam'"):
            RouterGraphQLHandler(app)

    async def test_same_name_different_domains_builds_and_executes(self):
        """D1: names must only clash WITHIN a domain group (one object type).

        iam.get_user and admin.iam.get_user are different GraphQL object
        types' fields — both must build and both must route to their own
        endpoint.
        """
        app = FastAPI()

        @app.get("/users/{user_id}", response_model=ItemOut, tags=["iam"])
        async def get_user(user_id: int):
            return ItemOut(id=user_id, name="public")

        @app.get("/admin/users/{user_id}", response_model=ItemOut, tags=["admin:iam"])
        async def get_user(user_id: int):  # noqa: F811 — same name, other domain
            return ItemOut(id=user_id, name="admin")

        handler = RouterGraphQLHandler(app)
        result = await handler.execute(
            "{ iam { get_user(user_id: 1) { name } }"
            " admin { iam { get_user(user_id: 2) { name } } } }"
        )
        assert result["data"]["iam"]["get_user"] == {"name": "public"}
        assert result["data"]["admin"]["iam"]["get_user"] == {"name": "admin"}

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


class TestResponseFilterPassthrough:
    """Routes whose serialization kwargs reshape the response JSON
    (exclude_unset / exclude_defaults / include / exclude /
    by_alias=False) stay in the schema as JSON-scalar fields instead of
    being skipped: filtering happens after validation, so per-field
    non-null promises cannot hold — the JSON blob carries whatever
    arrives, and the field description tells agents why.
    exclude_none is exempt: it only drops Optional-valued keys, which the
    bridge maps to nullable fields anyway."""

    class Filtered(BaseModel):
        id: int
        tag: str = "t"

    @staticmethod
    def _handler(**route_kwargs) -> RouterGraphQLHandler:
        app = FastAPI()

        @app.get("/x", response_model=TestResponseFilterPassthrough.Filtered,
                 tags=["g"], **route_kwargs)
        async def x() -> TestResponseFilterPassthrough.Filtered:
            return TestResponseFilterPassthrough.Filtered(id=1)

        return RouterGraphQLHandler(app)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"response_model_exclude_unset": True},
            {"response_model_exclude_defaults": True},
            {"response_model_include": {"id"}},
            {"response_model_exclude": {"tag"}},
            {"response_model_by_alias": False},
        ],
        ids=["unset", "defaults", "include", "exclude", "by-alias"],
    )
    def test_filtered_response_bridges_as_json(self, kwargs):
        handler = self._handler(**kwargs)
        sdl = handler.get_sdl()
        assert handler.skips == []
        assert "x: JSON" in sdl
        assert "raw JSON blob" in sdl  # agent-facing note in the field description

    def test_exclude_none_keeps_structured_type(self):
        handler = self._handler(response_model_exclude_none=True)
        assert handler.skips == []
        assert "x: Filtered" in handler.get_sdl()

    async def test_filtered_route_executes_without_null_violation(self):
        """The G1 bug: exclude_unset dropped defaulted keys and the non-null
        field nulled the whole object. As JSON the same query just returns
        the filtered blob."""
        handler = self._handler(response_model_exclude_unset=True)
        result = await handler.execute("{ g { x } }")
        assert result == {"data": {"g": {"x": {"id": 1}}}}  # tag dropped by the app
        await handler.aclose()

    async def test_filter_detected_through_include_router(self):
        from fastapi import APIRouter

        inner = APIRouter()

        @inner.get("/sub", response_model=TestResponseFilterPassthrough.Filtered,
                   response_model_exclude_unset=True, tags=["h"])
        async def sub() -> TestResponseFilterPassthrough.Filtered:
            return TestResponseFilterPassthrough.Filtered(id=2)

        app = FastAPI()
        app.include_router(inner, prefix="/api")
        handler = RouterGraphQLHandler(app)
        assert "sub: JSON" in handler.get_sdl()
        await handler.aclose()


class TestUnionFallback:
    """Non-Optional unions (``Item | Error``) pick their member at runtime,
    so the bridge falls back to the JSON scalar instead of skipping the
    route — with FIELD-level granularity: only the union part degrades,
    the surrounding model stays selectable. A real GraphQLUnionType with
    resolve_type remains a future upgrade; JSON never blocks it. Input-side
    (request body) unions still skip: GraphQL has no input unions."""

    class Err(BaseModel):
        code: int
        message: str

    @staticmethod
    def _handler() -> RouterGraphQLHandler:
        union = ItemOut | TestUnionFallback.Err

        class Wrapped(BaseModel):
            result: union  # type: ignore[valid-type]

        app = FastAPI()

        @app.get("/risky", tags=["u"])
        async def risky(ok: bool = True) -> union:  # type: ignore[valid-type]
            return (
                ItemOut(id=1, name="n")
                if ok
                else TestUnionFallback.Err(code=400, message="bad")
            )

        @app.get("/wrapped", response_model=Wrapped, tags=["u"])
        async def wrapped() -> Wrapped:
            return Wrapped(result=ItemOut(id=2, name="w"))

        @app.get("/mixed", tags=["u"])
        async def mixed() -> list[union]:  # type: ignore[valid-type]
            return [ItemOut(id=3, name="m"), TestUnionFallback.Err(code=404, message="x")]

        @app.get("/scalar-union", tags=["u"])
        async def scalar_union() -> int | str:
            return 7

        return RouterGraphQLHandler(app)

    def test_three_shapes_bridged_not_skipped(self):
        handler = self._handler()
        assert handler.skips == []
        sdl = handler.get_sdl()
        assert "risky(ok: Boolean = true): JSON" in sdl
        assert "result: JSON!" in sdl       # field-level: only the union degrades
        assert "mixed: [JSON!]" in sdl
        assert "scalar_union: JSON" in sdl  # scalar unions ride the same fallback

    def test_descriptions_name_the_members(self):
        sdl = self._handler().get_sdl()
        assert "shape is one of: ItemOut, Err" in sdl           # route-level note
        assert "shape is one of: ItemOut, Err (union field" in sdl  # nested-field note

    async def test_union_route_executes_both_members(self):
        handler = self._handler()
        result = await handler.execute(
            "{ u { risky(ok: false) wrapped { result } mixed } }"
        )
        assert result == {
            "data": {
                "u": {
                    "risky": {"code": 400, "message": "bad"},
                    "wrapped": {"result": {"id": 2, "name": "w"}},
                    "mixed": [
                        {"id": 3, "name": "m"},
                        {"code": 404, "message": "x"},
                    ],
                }
            }
        }
        await handler.aclose()

    def test_optional_union_stays_nullable_json(self):
        from fastapi_gql_mcp.type_builder import TypeBuilder

        assert render_type(TypeBuilder().output_type(int | str | None)) == "JSON"

    def test_input_side_union_still_unsupported(self):
        """Request-body unions keep skipping: GraphQL has no input unions."""
        app = FastAPI()

        @app.post("/u", tags=["u"])
        async def create(payload: ItemOut | TestUnionFallback.Err) -> dict:
            return {"ok": True}

        routes, skips = RouterScanner(app, allow_mutation=True).scan()
        assert routes == []
        assert any("Cannot map" in s.reason for s in skips)


def render_type(t) -> str:
    return str(t)


class TestDeprecation:
    """OpenAPI deprecated=True maps onto GraphQL-native deprecation: SDL
    directive, introspection visibility (hidden unless includeDeprecated),
    and executability are all spec behavior worth pinning."""

    @staticmethod
    def _handler() -> RouterGraphQLHandler:
        app = FastAPI()

        @app.get("/old", response_model=ItemOut, deprecated=True, tags=["misc"])
        async def old() -> ItemOut:
            return ItemOut(id=1, name="old")

        @app.get("/new", response_model=ItemOut, tags=["misc"])
        async def new() -> ItemOut:
            return ItemOut(id=2, name="new")

        return RouterGraphQLHandler(app)

    def test_sdl_carries_deprecation_directive(self):
        sdl = self._handler().get_sdl()
        assert 'old: ItemOut @deprecated(reason: "This endpoint is deprecated.")' in sdl
        assert "new: ItemOut" in sdl  # undeprecated fields stay bare

    async def test_introspection_hides_unless_requested(self):
        handler = self._handler()
        result = await handler.execute(
            '{ __type(name: "MiscQuery") { fields { name } '
            'all: fields(includeDeprecated: true) { name isDeprecated deprecationReason } } }'
        )
        visible = [f["name"] for f in result["data"]["__type"]["fields"]]
        assert visible == ["new"]  # spec default: deprecated fields are hidden
        by_name = {f["name"]: f for f in result["data"]["__type"]["all"]}
        assert by_name["old"]["isDeprecated"] is True
        assert by_name["old"]["deprecationReason"] == "This endpoint is deprecated."
        assert by_name["new"]["isDeprecated"] is False
        await handler.aclose()

    async def test_deprecated_field_still_executes(self):
        handler = self._handler()
        result = await handler.execute("{ misc { old { id } new { id } } }")
        assert result == {"data": {"misc": {"old": {"id": 1}, "new": {"id": 2}}}}
        await handler.aclose()


class TestMutationOnlyApp:
    def test_mutation_only_schema_fails_fast(self):
        from fastapi_gql_mcp import GQLMCPConfigError

        app = FastAPI()

        @app.post("/things", response_model=ItemOut)
        async def create_thing(payload: ItemCreate):
            return ItemOut(id=1, name=payload.name)

        with pytest.raises(GQLMCPConfigError, match="mutation-only"):
            RouterGraphQLHandler(app, allow_mutation=True)


class TestSkips:
    """R2: the scanner's skip report is caller-visible, not log-only."""

    def test_untyped_route_bridges_with_note(self):
        """Untyped endpoints bridge as raw JSON with an advisory note."""
        app = FastAPI()

        @app.get("/now")
        async def now():  # untyped on purpose: no response_model, no annotation
            return {"ts": 1}

        h = RouterGraphQLHandler(app)
        assert h.skips == []
        sdl = h.get_sdl()
        assert "now: JSON" in sdl
        assert "declares no response type" in sdl

    def test_no_skips_on_clean_app(self):
        h = RouterGraphQLHandler(build_app(), allow_mutation=True)
        assert h.skips == []
