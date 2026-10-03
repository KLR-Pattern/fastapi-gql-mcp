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

    async def test_headers_provider_sync(self):
        from typing import Annotated

        from fastapi import Depends, Header

        app = FastAPI()

        def auth(x_token: Annotated[str | None, Header()] = None):
            return x_token

        @app.get("/me", response_model=ItemOut)
        async def me(user=Depends(auth)):
            if user != "secret":
                from fastapi import HTTPException

                raise HTTPException(status_code=401, detail="unauthorized")
            return ItemOut(id=1, name="me")

        r = route_for(app, "GET", "/me")

        invoker = RouteInvoker(app, manage_lifespan=False)  # no provider -> 401
        with pytest.raises(GraphQLError) as exc:
            await invoker.invoke(r, {})
        assert exc.value.extensions["code"] == "HTTP_401"
        await invoker.aclose()

        invoker = RouteInvoker(
            app, headers_provider=lambda: {"x-token": "secret"}, manage_lifespan=False
        )
        assert await invoker.invoke(r, {}) == {"id": 1, "name": "me"}
        await invoker.aclose()

    async def test_headers_provider_async(self):
        from typing import Annotated

        from fastapi import Depends, Header

        app = FastAPI()

        def auth(x_token: Annotated[str | None, Header()] = None):
            return x_token

        @app.get("/me", response_model=ItemOut)
        async def me(user=Depends(auth)):
            if user != "async-token":
                from fastapi import HTTPException

                raise HTTPException(status_code=401, detail="unauthorized")
            return ItemOut(id=1, name="me")

        r = route_for(app, "GET", "/me")
        invoker = RouteInvoker(
            app,
            headers_provider=async_provider,
            manage_lifespan=False,
        )
        assert await invoker.invoke(r, {}) == {"id": 1, "name": "me"}
        await invoker.aclose()


async def async_provider() -> dict[str, str]:
    return {"x-token": "async-token"}
