"""scanner: FastAPI routes -> RouteInfo / SkipRecord."""

from enum import Enum
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from fastapi_gql_mcp.scanner import RouterScanner, SkipRecord


class ItemOut(BaseModel):
    id: int
    name: str


class ItemCreate(BaseModel):
    name: str


def build_app() -> FastAPI:
    app = FastAPI()

    def dep_filter(active: bool = Query(True)):
        return active

    @app.get("/items", response_model=list[ItemOut], tags=["shop:catalog"])
    async def list_items(active: bool = Depends(dep_filter), limit: int = Query(10)):
        return [ItemOut(id=1, name="a")] * limit

    @app.get("/items/{item_id}", response_model=ItemOut, tags=["shop:catalog"])
    async def get_item(item_id: int):
        return ItemOut(id=item_id, name="a")

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    @app.get("/health", response_model=ItemOut)
    async def health():
        return ItemOut(id=0, name="ok")

    @app.post("/items", response_model=ItemOut)
    async def create_item(payload: ItemCreate):
        return ItemOut(id=2, name=payload.name)

    @app.post("/bulk", response_model=list[ItemOut])
    async def create_bulk(a: ItemCreate, b: ItemCreate):
        return []

    @app.get("/raw")
    async def raw() -> PlainTextResponse:
        return PlainTextResponse("x")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(iter(["x"]))

    @app.get("/hidden", include_in_schema=False, response_model=ItemOut)
    async def hidden():
        return ItemOut(id=0, name="h")

    @app.get("/needs-header")
    async def needs_header(x_token: Annotated[str, Header()]):
        return {"ok": True}

    @app.get("/optional-header")
    async def optional_header(x_opt: Annotated[str | None, Header()] = None):
        return {"ok": True}

    @app.patch("/items/{item_id}", response_model=ItemOut)
    async def patch_item(item_id: int, payload: ItemCreate):
        return ItemOut(id=item_id, name=payload.name)

    return app


def scan(app, **kw):
    return RouterScanner(app, **kw).scan()


def by_field(routes, name):
    return next(r for r in routes if r.field_name == name)


def skip_reasons(skips, path):
    return [s.reason for s in skips if s.path == path]


class TestDiscovery:
    def test_get_routes_discovered(self):
        routes, _ = scan(build_app())
        names = [r.field_name for r in routes]
        assert "list_items" in names
        assert "get_item" in names  # function names, no verb/param rewriting

    def test_field_names(self):
        routes, _ = scan(build_app(), allow_mutation=True)
        assert by_field(routes, "list_items").path == "/items"
        assert by_field(routes, "create_item").method == "POST"

    def test_path_params_required(self):
        routes, _ = scan(build_app())
        r = by_field(routes, "get_item")
        assert r is not None

    def test_depends_query_params_merged(self):
        routes, _ = scan(build_app())
        r = by_field(routes, "list_items")
        qnames = [p.name for p in r.query_params]
        assert "active" in qnames and "limit" in qnames


class TestMutationGating:
    def test_mutations_skipped_by_default(self):
        routes, skips = scan(build_app())
        assert all(not r.is_mutation for r in routes)
        assert any("/items" in s.path and s.method == "POST" for s in skips)

    def test_mutations_included_when_allowed(self):
        routes, skips = scan(build_app(), allow_mutation=True)
        assert by_field(routes, "create_item").is_mutation
        assert by_field(routes, "patch_item").method == "PATCH"


class TestFiltering:
    def test_include_glob(self):
        routes, _ = scan(build_app(), include=["/items*"])
        assert {r.path for r in routes} <= {"/items", "/items/{item_id}"}

    def test_exclude_glob(self):
        routes, _ = scan(build_app(), exclude=["/ping", "/raw", "/stream"])
        assert all(r.path not in {"/ping", "/raw", "/stream"} for r in routes)

    def test_exclude_wins_over_include(self):
        routes, _ = scan(build_app(), include=["/items*"], exclude=["/items/{item_id}"])
        assert "/items/{item_id}" not in {r.path for r in routes}


class Color(Enum):  # non-str Enum tags must be ignored by tag filtering
    red = "red"


def build_tagged_app() -> FastAPI:
    app = FastAPI()

    @app.get("/users", response_model=ItemOut, tags=["iam:users"])
    async def users():
        return ItemOut(id=1, name="a")

    @app.get("/invoices", response_model=ItemOut, tags=["iam:billing"])
    async def invoices():
        return ItemOut(id=2, name="b")

    @app.get("/reports", response_model=ItemOut, tags=["analytics"])
    async def reports():
        return ItemOut(id=3, name="c")

    @app.get("/untagged")
    async def untagged():
        return {"ok": True}

    @app.get("/enum-tag", tags=[Color.red])
    async def enum_tag():
        return {"ok": True}

    @app.post("/users", response_model=ItemOut, tags=["iam:users"])
    async def create_user(payload: ItemCreate):
        return ItemOut(id=4, name=payload.name)

    return app


class TestTagFiltering:
    def test_include_tags_glob_prefix(self):
        routes, _ = scan(build_tagged_app(), include_tags=["iam:*"])
        assert {r.path for r in routes} == {"/users", "/invoices"}

    def test_include_tags_exact(self):
        routes, _ = scan(build_tagged_app(), include_tags=["analytics"])
        assert {r.path for r in routes} == {"/reports"}

    def test_include_tags_drops_untagged_silently(self):
        routes, skips = scan(build_tagged_app(), include_tags=["iam:*"])
        paths = {r.path for r in routes}
        assert "/untagged" not in paths and "/enum-tag" not in paths
        assert not any(s.path in {"/untagged", "/enum-tag"} for s in skips)

    def test_exclude_tags(self):
        routes, _ = scan(build_tagged_app(), exclude_tags=["iam:*"])
        assert {r.path for r in routes} == {"/reports", "/untagged", "/enum-tag"}

    def test_exclude_tags_wins_over_include_tags(self):
        routes, _ = scan(
            build_tagged_app(), include_tags=["iam:*"], exclude_tags=["iam:billing"]
        )
        assert {r.path for r in routes} == {"/users"}

    def test_tag_filters_and_with_path_filters(self):
        routes, _ = scan(
            build_tagged_app(), include=["/users", "/invoices"], include_tags=["analytics"]
        )
        assert routes == []
        routes, _ = scan(
            build_tagged_app(), include=["/users", "/reports"], include_tags=["iam:*"]
        )
        assert {r.path for r in routes} == {"/users"}

    def test_enum_tag_ignored(self):
        routes, _ = scan(build_tagged_app())  # no filter: enum-tag route scans fine
        assert "/enum-tag" in {r.path for r in routes}
        routes, _ = scan(build_tagged_app(), include_tags=["iam:*"])
        assert "/enum-tag" not in {r.path for r in routes}

    def test_include_tags_empty_drops_everything(self):
        routes, skips = scan(build_tagged_app(), include_tags=[])
        assert routes == [] and skips == []

    def test_tag_filter_precedes_mutation_gating(self):
        # POST /users is tag-filtered out before mutation gating: no skip.
        _, skips = scan(build_tagged_app(), include_tags=["iam:billing"])
        assert not any(s.method == "POST" for s in skips)
        routes, _ = scan(
            build_tagged_app(), include_tags=["iam:*"], allow_mutation=True
        )
        assert by_field(routes, "create_user").is_mutation

    def test_no_tag_skip_records(self):
        _, skips = scan(
            build_tagged_app(),
            include_tags=["iam:*"],
            exclude_tags=["iam:billing"],
        )
        assert not any("tag" in s.reason.lower() for s in skips)


class TestSkips:
    def test_untyped_response_bridges_as_any(self):
        routes, skips = scan(build_app())
        assert by_field(routes, "ping").response_annotation is Any
        assert not any("no typed response" in r for r in skip_reasons(skips, "/ping"))

    def test_raw_response_skipped(self):
        _, skips = scan(build_app())
        assert any("raw Response" in r for r in skip_reasons(skips, "/raw"))
        assert any("raw Response" in r for r in skip_reasons(skips, "/stream"))

    def test_hidden_route_skipped(self):
        _, skips = scan(build_app())
        assert any("hidden" in r for r in skip_reasons(skips, "/hidden"))

    def test_hidden_route_included_when_asked(self):
        routes, _ = scan(build_app(), include_hidden=True)
        assert any(r.path == "/hidden" for r in routes)

    def test_required_header_skipped(self):
        _, skips = scan(build_app())
        assert any("header/cookie" in r for r in skip_reasons(skips, "/needs-header"))

    def test_form_only_scalar_skipped(self):
        """G12 regression: Annotated[str, Form()] has a perfectly valid
        GraphQL annotation, so the type check alone would let it through
        into a field that always 422s at runtime (invoker sends JSON)."""
        from fastapi import Form

        app = FastAPI()

        @app.post("/ff", response_model=ItemOut)
        async def ff(name: Annotated[str, Form()]):
            return ItemOut(id=1, name=name)

        routes, skips = scan(app, allow_mutation=True)
        assert routes == []
        assert any(
            "form/file parameter 'name'" in r for r in skip_reasons(skips, "/ff")
        )

    def test_optional_header_does_not_trigger_header_skip(self):
        # Optional headers are simply not sent; the route is skipped for its
        # untyped response instead, never for the optional header itself.
        _, skips = scan(build_app())
        assert not any("header/cookie" in r for r in skip_reasons(skips, "/optional-header"))


class TestParams:
    def test_query_param_defaults(self):
        routes, _ = scan(build_app())
        r = by_field(routes, "list_items")
        limit = next(p for p in r.query_params if p.name == "limit")
        assert limit.required is False
        assert limit.default == 10
        active = next(p for p in r.query_params if p.name == "active")
        assert active.required is False  # dep default True... depends' Query(True)

    def test_single_body_not_embedded(self):
        routes, _ = scan(build_app(), allow_mutation=True)
        r = by_field(routes, "create_item")
        assert len(r.body_params) == 1
        assert r.body_params[0].embed is False

    def test_multiple_bodies_embedded(self):
        routes, _ = scan(build_app(), allow_mutation=True)
        r = by_field(routes, "create_bulk")
        assert all(p.embed for p in r.body_params)

    def test_response_annotation(self):
        routes, _ = scan(build_app())
        assert by_field(routes, "get_item").response_annotation is ItemOut

    def test_deprecated_flag_captured(self):
        app = FastAPI()

        @app.get("/old", response_model=ItemOut, deprecated=True)
        async def old():
            return ItemOut(id=1, name="o")

        @app.get("/new", response_model=ItemOut)
        async def new():
            return ItemOut(id=2, name="n")

        routes, _ = scan(app)
        assert by_field(routes, "old").deprecated is True
        assert by_field(routes, "new").deprecated is False


class TestDomains:
    def test_tag_domains(self):
        routes, _ = scan(build_app())
        assert by_field(routes, "list_items").domains == frozenset({("shop", "catalog")})

    def test_untagged_falls_back_to_path(self):
        routes, _ = scan(build_app())
        assert by_field(routes, "health").domains == frozenset({("health",)})


class TestTypeTrials:
    def test_unsupported_response_skipped(self):
        app = FastAPI()

        @app.get("/weird")
        async def weird() -> set[int]:
            # Pydantic accepts it, the bridge has no scalar for it
            return set()

        routes, skips = RouterScanner(app).scan()
        assert not routes
        assert any("unsupported type" in s.reason for s in skips)

    def test_dict_response_bridged_as_json(self):
        """dict/Any declare a dynamic shape — they pass through, not skip."""
        app = FastAPI()

        @app.get("/raw")
        async def raw() -> dict[str, int]:
            return {}

        routes, skips = RouterScanner(app).scan()
        assert len(routes) == 1 and not skips

    def test_skip_record_shape(self):
        rec = SkipRecord("/x", "GET", "why")
        assert (rec.path, rec.method, rec.reason) == ("/x", "GET", "why")


class TestMountAndSockets:
    def test_websocket_and_mount_ignored(self):
        from fastapi import WebSocket

        app = FastAPI()
        sub = FastAPI()

        @sub.get("/inner", response_model=ItemOut)
        async def inner():
            return ItemOut(id=1, name="i")

        app.mount("/sub", sub)

        @app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()

        routes, skips = RouterScanner(app).scan()
        assert routes == []
        assert skips == []


class TestJsonFallbackNotices:
    """Routes degraded to raw JSON (no field selection) get startup notices
    naming the CAUSE and the way out — a silent downgrade is an
    undiagnosable downgrade. Top-level union responses and serialization
    filters report per-route; union fields nested in models report per-field."""

    class Err(BaseModel):
        code: int
        message: str

    def test_union_response_notice_names_members_and_remedy(self, caplog):
        import logging

        app = FastAPI()

        @app.get("/risky", tags=["u"])
        async def risky(ok: bool = True) -> ItemOut | TestJsonFallbackNotices.Err:
            return ItemOut(id=1, name="n")

        with caplog.at_level(logging.WARNING, logger="fastapi_gql_mcp.scanner"):
            routes, _ = scan(app)
        assert len(routes) == 1  # degraded, not skipped
        rendered = caplog.text
        assert "bridged 1 route(s) as raw JSON" in rendered
        assert "GET /risky: union response (ItemOut|Err)" in rendered
        assert "restructure into one model per shape" in rendered

    def test_nested_union_field_notice_names_the_field(self, caplog):
        import logging

        class Wrapped(BaseModel):
            result: ItemOut | TestJsonFallbackNotices.Err

        app = FastAPI()

        @app.get("/wrapped", response_model=Wrapped, tags=["u"])
        async def wrapped() -> Wrapped:
            return Wrapped(result=ItemOut(id=1, name="w"))

        with caplog.at_level(logging.WARNING, logger="fastapi_gql_mcp.scanner"):
            scan(app)
        assert "bridged 1 union field(s)" in caplog.text
        assert "Wrapped.result (ItemOut, Err)" in caplog.text
        assert "restructure the model class" in caplog.text

    def test_response_filter_notice(self, caplog):
        import logging

        app = FastAPI()

        @app.get("/sparse", response_model=ItemOut,
                 response_model_exclude_unset=True)
        async def sparse() -> ItemOut:
            return ItemOut(id=1, name="s")

        with caplog.at_level(logging.WARNING, logger="fastapi_gql_mcp.scanner"):
            scan(app)
        assert "response filtered via response_model_exclude_unset" in caplog.text

    def test_no_notice_for_structured_routes(self, caplog):
        import logging

        app = FastAPI()

        @app.get("/clean", response_model=ItemOut)
        async def clean() -> ItemOut:
            return ItemOut(id=1, name="c")

        with caplog.at_level(logging.WARNING, logger="fastapi_gql_mcp.scanner"):
            scan(app)
        assert "raw JSON" not in caplog.text


class TestQueryParameterModels:
    def test_lone_query_model_expanded(self):
        from typing import Annotated

        from fastapi import Query

        class ItemFilter(BaseModel):
            category: str
            min_price: float = 0.0
            page_size: int = Field(default=20, validation_alias="pageSize")

        app = FastAPI()

        @app.get("/filtered", response_model=ItemOut, tags=["demo"])
        async def filtered(filters: Annotated[ItemFilter, Query()]):
            return ItemOut(id=1, name="x")

        routes, skips = RouterScanner(app).scan()
        assert not skips
        r = by_field(routes, "filtered")
        q = {p.name: p for p in r.query_params}
        assert set(q) == {"category", "min_price", "pageSize"}
        assert q["category"].required is True
        assert q["min_price"].default == 0.0
        assert q["pageSize"].default == 20

    async def test_query_model_end_to_end(self):
        from typing import Annotated

        from fastapi import Query

        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        class ItemFilter(BaseModel):
            category: str
            limit: int = 2

        app = FastAPI()
        seen: dict = {}

        @app.get("/things", response_model=list[ItemOut], tags=["demo"])
        async def things(filters: Annotated[ItemFilter, Query()]):
            seen.update(filters.model_dump())
            return [ItemOut(id=i, name=filters.category) for i in range(filters.limit)]

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        assert "things(category: String!, limit: Int = 2): [ItemOut!]" in sdl
        result = await handler.execute(
            "{ demo { things(category: \"tools\") { name } } }"
        )
        assert result == {"data": {"demo": {"things": [{"name": "tools"}, {"name": "tools"}]}}}
        assert seen == {"category": "tools", "limit": 2}
        await handler.aclose()

    def test_query_model_mixed_with_plain_param_skipped(self):
        from typing import Annotated

        from fastapi import Query

        class ItemFilter(BaseModel):
            category: str

        app = FastAPI()

        @app.get("/mixed", response_model=ItemOut)
        async def mixed(filters: Annotated[ItemFilter, Query()], limit: int = 5):
            return ItemOut(id=1, name="x")

        routes, skips = RouterScanner(app).scan()
        assert routes == []
        assert any("mixed with" in s.reason for s in skips)


class TestIncludeRouter:
    def test_routers_via_include_router_are_discovered(self):
        from fastapi import APIRouter

        inner = APIRouter()

        @inner.get("/inner", response_model=ItemOut)
        async def inner_route():
            return ItemOut(id=1, name="i")

        outer = APIRouter(prefix="/outer")
        outer.include_router(inner)  # nested include

        @outer.get("/own", response_model=ItemOut)
        async def outer_route():
            return ItemOut(id=2, name="o")

        app = FastAPI()
        app.include_router(outer)

        routes, skips = RouterScanner(app).scan()
        assert not skips
        assert {r.path for r in routes} == {"/outer/inner", "/outer/own"}
        assert {r.field_name for r in routes} == {"inner_route", "outer_route"}
