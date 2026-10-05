"""invoker: request planning + in-process ASGI execution."""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from graphql import GraphQLError
from pydantic import BaseModel

from fastapi_gql_mcp.invoker import GQLMCPRuntimeError, RouteInvoker, build_request
from fastapi_gql_mcp.scanner import RouterScanner


class ItemOut(BaseModel):
    id: int
    name: str


class ItemCreate(BaseModel):
    name: str
    price: float = 9.9


def build_app() -> tuple[FastAPI, list[str]]:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        events.append("startup")
        yield
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)

    @app.get("/items/{item_id}", response_model=ItemOut)
    async def get_item(item_id: int):
        return ItemOut(id=item_id, name="x")

    @app.get("/search", response_model=list[ItemOut])
    async def search(q: str | None = None, limit: int = 5, active: bool = True):
        return [ItemOut(id=i, name=q or "-") for i in range(limit if active else 0)]

    @app.post("/items", response_model=ItemOut)
    async def create_item(payload: ItemCreate):
        return ItemOut(id=7, name=payload.name)

    @app.post("/bulk", response_model=list[ItemOut])
    async def create_bulk(a: ItemCreate, b: ItemCreate):
        return [ItemOut(id=1, name=a.name), ItemOut(id=2, name=b.name)]

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kapow")

    return app, events


def route_for(app, method, path):
    routes, _ = RouterScanner(app, allow_mutation=True).scan()
    return next(r for r in routes if r.method == method and r.path == path)


class TestBuildRequest:
    def test_path_fill_and_query(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        plan = build_request(r, {"item_id": 42, "unused": 1})
        assert plan.path == "/items/42"
        assert plan.params == ()
        assert plan.json_body is None

    def test_query_none_dropped_bool_rendered(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/search")
        plan = build_request(r, {"q": None, "limit": 3, "active": False})
        assert plan.params == (("limit", "3"), ("active", "false"))

    def test_query_default_used_when_absent(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/search")
        plan = build_request(r, {})
        assert dict(plan.params) == {"limit": "5", "active": "true"}

    def test_single_body_not_embedded(self):
        app, _ = build_app()
        r = route_for(app, "POST", "/items")
        plan = build_request(r, {"payload": {"name": "n", "price": 1.0}})
        assert plan.json_body == {"name": "n", "price": 1.0}

    def test_multiple_bodies_embedded(self):
        app, _ = build_app()
        r = route_for(app, "POST", "/bulk")
        plan = build_request(r, {"a": {"name": "a"}, "b": {"name": "b"}})
        assert plan.json_body == {"a": {"name": "a"}, "b": {"name": "b"}}

    def test_extra_headers_merged(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        plan = build_request(r, {"item_id": 1}, {"authorization": "Bearer t"})
        assert plan.headers["authorization"] == "Bearer t"
        assert plan.headers["accept"] == "application/json"

    def test_missing_path_param_raises_graphql_error(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        with pytest.raises(GraphQLError, match="path parameter 'item_id'"):
            build_request(r, {})


class TestLifecycle:
    async def test_lifespan_lazy_and_idempotent(self):
        app, events = build_app()
        invoker = RouteInvoker(app)
        assert events == []  # not started yet
        r = route_for(app, "GET", "/items/{item_id}")
        await invoker.invoke(r, {"item_id": 1})
        assert events == ["startup"]
        await invoker.invoke(r, {"item_id": 2})
        assert events == ["startup"]  # still exactly one startup
        await invoker.aclose()
        assert events == ["startup", "shutdown"]

    async def test_closed_invoker_rejects_use(self):
        app, _ = build_app()
        invoker = RouteInvoker(app)
        await invoker.aclose()
        with pytest.raises(GQLMCPRuntimeError):
            await invoker.start()

    async def test_no_managed_lifespan(self):
        app, events = build_app()
        invoker = RouteInvoker(app, manage_lifespan=False)
        r = route_for(app, "GET", "/items/{item_id}")
        result = await invoker.invoke(r, {"item_id": 1})
        assert result == {"id": 1, "name": "x"}
        assert events == []


class TestInvoke:
    async def test_invoke_returns_json(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        invoker = RouteInvoker(app, manage_lifespan=False)
        assert await invoker.invoke(r, {"item_id": 9}) == {"id": 9, "name": "x"}
        await invoker.aclose()

    async def test_invoke_body_route(self):
        app, _ = build_app()
        r = route_for(app, "POST", "/items")
        invoker = RouteInvoker(app, manage_lifespan=False)
        result = await invoker.invoke(r, {"payload": {"name": "n"}})
        assert result == {"id": 7, "name": "n"}
        await invoker.aclose()

    async def test_422_maps_to_http_error(self):
        app, _ = build_app()
        r = route_for(app, "GET", "/items/{item_id}")
        invoker = RouteInvoker(app, manage_lifespan=False)
        # int coercion happens at the GraphQL layer; str id -> FastAPI 422
        with pytest.raises(GraphQLError) as exc:
            await invoker.invoke(r, {"item_id": "not-an-int"})
        assert exc.value.extensions["code"] == "HTTP_422"
        await invoker.aclose()


class TestPathConvertorRoutes:
    """B1 regression: URLs must be rendered by Starlette (url_path_for), not
    by string surgery on route.path — convertor syntax {id:int} / {p:path}
    made hand-rolled replacement miss and 404 on the literal template."""

    @staticmethod
    def build_app() -> FastAPI:
        class Got(BaseModel):
            got: str

        app = FastAPI()

        @app.get("/conv/{item_id:int}", response_model=Got)
        async def get_conv(item_id: int) -> Got:
            return Got(got=f"id={item_id}")

        @app.get("/files/{p:path}", response_model=Got)
        async def get_file(p: str) -> Got:
            return Got(got=f"p={p}")

        @app.get("/城市/{name}", response_model=Got)
        async def get_city(name: str) -> Got:
            return Got(got=f"city={name}")

        return app

    async def _invoke(self, path: str, kwargs: dict) -> dict:
        app = self.build_app()
        routes, _ = RouterScanner(app).scan()
        route = next(r for r in routes if r.path == path)
        invoker = RouteInvoker(app, manage_lifespan=False)
        try:
            return await invoker.invoke(route, kwargs)
        finally:
            await invoker.aclose()

    async def test_int_convertor_b1_regression(self):
        assert await self._invoke("/conv/{item_id:int}", {"item_id": 5}) == {"got": "id=5"}

    async def test_path_convertor_with_spaces(self):
        assert await self._invoke("/files/{p:path}", {"p": "docs/read me.pdf"}) == {
            "got": "p=docs/read me.pdf"
        }

    async def test_unicode_path_segment(self):
        assert await self._invoke("/城市/{name}", {"name": "上海"}) == {"got": "city=上海"}

    async def test_negative_int_is_field_error_not_crash(self):
        # IntegerConvertor asserts on negatives; surfaced as BAD_REQUEST.
        with pytest.raises(GraphQLError) as exc:
            await self._invoke("/conv/{item_id:int}", {"item_id": -1})
        assert exc.value.extensions["code"] == "BAD_REQUEST"


class TestTimeoutEnforcement:
    """R1: httpx timeouts are INERT on ASGITransport — asyncio.wait_for in
    invoke() is the real enforcement, wired through request_timeout."""

    @staticmethod
    def _slow_app(seconds: float) -> FastAPI:
        import asyncio

        from pydantic import BaseModel

        class Ok(BaseModel):
            ok: bool

        app = FastAPI()

        @app.get("/slow", response_model=Ok)
        async def slow() -> Ok:
            await asyncio.sleep(seconds)
            return Ok(ok=True)

        return app

    async def test_slow_route_times_out(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        handler = RouterGraphQLHandler(self._slow_app(0.5), request_timeout=0.05)
        assert handler.invoker.timeout == 0.05
        result = await handler.execute("{ slow { slow { ok } } }")
        assert "errors" in result
        assert "timed out after 0.05s" in result["errors"][0]["message"]
        assert result["errors"][0]["extensions"]["code"] == "TIMEOUT"
        await handler.aclose()

    async def test_none_disables_enforcement(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        handler = RouterGraphQLHandler(self._slow_app(0.2), request_timeout=None)
        result = await handler.execute("{ slow { slow { ok } } }")
        assert result == {"data": {"slow": {"slow": {"ok": True}}}}
        await handler.aclose()


class TestProtocolHeaderProtection:
    """R4: content-type/accept are refused even when whitelisted — a
    forwarded value would retype the JSON body request into a 422."""

    def test_protocol_headers_never_forwarded(self):
        from fastapi_gql_mcp.invoker import filter_passthrough_headers

        filtered = filter_passthrough_headers(
            {"Content-Type": "text/plain", "Accept": "*/*", "Authorization": "Bearer x"},
            ["content-type", "accept", "authorization"],
        )
        assert filtered == {"authorization": "Bearer x"}

    def test_custom_headers_still_forwarded(self):
        from fastapi_gql_mcp.invoker import filter_passthrough_headers

        assert filter_passthrough_headers({"X-Custom": "1"}, ["x-custom"]) == {
            "x-custom": "1"
        }


class TestConcurrencyLimits:
    """P0-2: sibling fields fan out concurrently; max_concurrency bounds it
    invoker-globally (the wrapped app's upstream is what needs protecting)."""

    @staticmethod
    def _fanout_app(n: int, state: dict) -> FastAPI:
        import asyncio

        from pydantic import BaseModel

        class Ok(BaseModel):
            ok: bool

        app = FastAPI()

        def make(i: int):
            async def endpoint() -> Ok:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
                await asyncio.sleep(0.15)
                state["current"] -= 1
                return Ok(ok=True)

            endpoint.__name__ = f"job_{i}"
            return endpoint

        for i in range(n):
            app.get(f"/job{i}", response_model=Ok, tags=["jobs"])(make(i))
        return app

    @staticmethod
    def _query(n: int) -> str:
        fields = " ".join(f"job_{i} {{ ok }}" for i in range(n))
        return f"{{ jobs {{ {fields} }} }}"

    async def test_fanout_bounded_by_max_concurrency(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        state = {"current": 0, "peak": 0}
        handler = RouterGraphQLHandler(self._fanout_app(4, state), max_concurrency=2)
        assert handler.invoker.max_concurrency == 2
        result = await handler.execute(self._query(4))
        assert result["data"]["jobs"]["job_3"] == {"ok": True}
        assert state["peak"] <= 2, f"fan-out exceeded the bound: {state}"
        await handler.aclose()

    async def test_unbounded_runs_in_parallel(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        state = {"current": 0, "peak": 0}
        handler = RouterGraphQLHandler(self._fanout_app(4, state), max_concurrency=None)
        result = await handler.execute(self._query(4))
        assert result["data"]["jobs"]["job_0"] == {"ok": True}
        assert state["peak"] >= 2, "sibling fields should resolve concurrently"
        await handler.aclose()

    def test_invalid_max_concurrency_rejected(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        with pytest.raises(ValueError, match="max_concurrency"):
            RouterGraphQLHandler(self._fanout_app(1, {}), max_concurrency=0)


class TestSameRouteAliasFanout:
    """One route queried several times via aliases: each alias resolves the
    SAME stateless resolver closure with its own kwargs (build_request is
    pure), so an aliased selection on one route fans out exactly like
    distinct-route siblings — concurrently, semaphore-bounded."""

    @staticmethod
    def _app(state: dict) -> FastAPI:
        import asyncio

        class Item(BaseModel):
            item_id: int

        app = FastAPI()

        @app.get("/items/{item_id}", response_model=Item, tags=["shop"])
        async def get_item(item_id: int) -> Item:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            await asyncio.sleep(0.15)
            state["current"] -= 1
            return Item(item_id=item_id)

        return app

    @staticmethod
    def _aliased(n: int) -> str:
        fields = " ".join(f"x{i}: get_item(item_id: {i}) {{ item_id }}" for i in range(n))
        return f"{{ shop {{ {fields} }} }}"

    async def test_aliases_concurrent_and_isolated(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        state = {"current": 0, "peak": 0}
        handler = RouterGraphQLHandler(self._app(state))
        result = await handler.execute(self._aliased(3))
        assert result == {
            "data": {
                "shop": {
                    "x0": {"item_id": 0},
                    "x1": {"item_id": 1},
                    "x2": {"item_id": 2},
                }
            }
        }
        assert state["peak"] == 3, "aliased siblings should resolve concurrently"
        await handler.aclose()

    async def test_alias_fanout_bounded_by_max_concurrency(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        state = {"current": 0, "peak": 0}
        handler = RouterGraphQLHandler(self._app(state), max_concurrency=2)
        result = await handler.execute(self._aliased(4))
        assert result["data"]["shop"] == {
            f"x{i}": {"item_id": i} for i in range(4)
        }
        assert state["peak"] <= 2, f"same-route fan-out escaped the bound: {state}"
        await handler.aclose()


class TestScalarBodyRoundTrip:
    """Custom scalars' parse_value yields typed objects (Decimal/UUID/
    datetime); the body must cross json.dumps — regression for the
    "Object of type Decimal is not JSON serializable" field error."""

    @staticmethod
    def _app() -> FastAPI:
        from datetime import datetime
        from decimal import Decimal
        from uuid import UUID

        from pydantic import BaseModel

        app = FastAPI()

        class Payment(BaseModel):
            amount: Decimal
            ref: UUID
            at: datetime
            lines: list[Decimal] = []

        @app.get("/ping", response_model=dict, tags=["misc"])
        async def ping() -> dict:
            return {"ok": True}

        @app.post("/pay", response_model=dict, tags=["misc"])
        async def pay(payload: Payment) -> dict:
            return {
                "amount": str(payload.amount),
                "ref": str(payload.ref),
                "at": payload.at.isoformat(),
                "n_lines": len(payload.lines),
            }

        return app

    async def test_body_with_decimal_uuid_datetime(self):
        from fastapi_gql_mcp.handler import RouterGraphQLHandler

        handler = RouterGraphQLHandler(self._app(), allow_mutation=True)
        result = await handler.execute(
            'mutation { misc { pay(payload: {'
            'amount: "12.34", '
            'ref: "12345678-1234-5678-1234-567812345678", '
            'at: "2026-10-05T10:00:00Z", '
            'lines: ["1.5", "2.5"]'
            '}) } }'
        )
        assert result == {
            "data": {
                "misc": {
                    "pay": {
                        "amount": "12.34",
                        "ref": "12345678-1234-5678-1234-567812345678",
                        "at": "2026-10-05T10:00:00+00:00",
                        "n_lines": 2,
                    }
                }
            }
        }
        await handler.aclose()
