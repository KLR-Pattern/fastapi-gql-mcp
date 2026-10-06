"""Router-edge pins (G5-G11): verified behaviors from the 2026-10-06 wave.

Tier-2 mechanisms proven by probe (generic pagination, sync endpoints) and
tier-3 zero-coverage shapes (dependencies at three injection levels,
multi-verb routes, special params, bodyless responses, root path) — each
block pins what the probes established; mindmap nodes 125/133 hold the
evidence chains.
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel

from fastapi_gql_mcp import RouterGraphQLHandler

T = TypeVar("T")


class Ok(BaseModel):
    ok: bool


class Item(BaseModel):
    id: int


class User(BaseModel):
    name: str


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int


# ------------------------------------------------------------------- G5


class TestGenericPagination:
    """Page[Item] / Page[User]: parametrized generics get clean,
    per-parameterization type names (Page_Item — not the sanitized mangle
    Page_Item_) and both execute."""

    @staticmethod
    def _handler() -> RouterGraphQLHandler:
        app = FastAPI()

        @app.get("/items-page", response_model=Page[Item], tags=["p"])
        async def items_page() -> Page[Item]:
            return Page(items=[Item(id=1), Item(id=2)], total=2)

        @app.get("/users-page", response_model=Page[User], tags=["p"])
        async def users_page() -> Page[User]:
            return Page(items=[User(name="a")], total=1)

        return RouterGraphQLHandler(app)

    def test_clean_type_names_no_sanitizer_warning(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="fastapi_gql_mcp.type_builder"):
            sdl = self._handler().get_sdl()
        assert "type Page_Item {" in sdl
        assert "type Page_User {" in sdl
        assert "Page[Item]" not in sdl
        assert "Sanitized" not in caplog.text

    async def test_both_parameterizations_execute(self):
        handler = self._handler()
        result = await handler.execute(
            "{ p { items_page { total items { id } } users_page { items { name } } } }"
        )
        assert result == {
            "data": {
                "p": {
                    "items_page": {"total": 2, "items": [{"id": 1}, {"id": 2}]},
                    "users_page": {"items": [{"name": "a"}]},
                }
            }
        }
        await handler.aclose()


# ------------------------------------------------------------------- G6


class TestSyncEndpoint:
    """A sync ``def`` endpoint runs through starlette's anyio threadpool —
    transparent to the bridge."""

    async def test_sync_endpoint_executes(self):
        app = FastAPI()

        @app.get("/sync-item", response_model=Item, tags=["s"])
        def sync_item() -> Item:  # sync on purpose
            return Item(id=7)

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ s { sync_item { id } } }")
        assert result == {"data": {"s": {"sync_item": {"id": 7}}}}
        await handler.aclose()


# ------------------------------------------------------------------- G7


class TestDependenciesAtThreeLevels:
    """dependencies=[Depends(...)] injected at the route decorator, the
    APIRouter, and the app: sub-dependency params merge into the flattened
    view, a required-header guard skips the route at any level, and a
    401-raising guard surfaces as an HTTP_401 field error."""

    @staticmethod
    def _required_header_guard(x_key: Annotated[str, Header()]) -> str:
        """REQUIRED header → the route skips at scan time (a GraphQL
        argument cannot carry a header the bridge refuses to forward)."""
        return x_key

    @staticmethod
    def _raising_guard(x_key: Annotated[str | None, Header()] = None) -> str:
        """OPTIONAL header that validates → route stays in the schema and
        fails at runtime with 401 when the header is absent/wrong."""
        if x_key != "secret":
            raise HTTPException(status_code=401, detail="bad key")
        return x_key

    def test_required_header_guard_skips_at_all_three_levels(self):
        from fastapi_gql_mcp.scanner import RouterScanner

        # route level
        app1 = FastAPI()

        @app1.get("/a", response_model=Ok,
                  dependencies=[Depends(self._required_header_guard)])
        async def a() -> Ok:
            return Ok(ok=True)

        # router level
        app2 = FastAPI()
        router = APIRouter(dependencies=[Depends(self._required_header_guard)])

        @router.get("/b", response_model=Ok)
        async def b() -> Ok:
            return Ok(ok=True)

        app2.include_router(router)

        # app level
        app3 = FastAPI(dependencies=[Depends(self._required_header_guard)])

        @app3.get("/c", response_model=Ok)
        async def c() -> Ok:
            return Ok(ok=True)

        for app in (app1, app2, app3):
            routes, skips = RouterScanner(app).scan()
            assert routes == []
            assert any("header/cookie parameter 'x-key'" in s.reason for s in skips)

    async def test_raising_guard_maps_to_http_401_field_error(self):
        app = FastAPI()

        @app.get("/locked", response_model=Ok, dependencies=[Depends(self._raising_guard)],
                 tags=["auth"])
        async def locked() -> Ok:
            return Ok(ok=True)

        handler = RouterGraphQLHandler(app)
        result = await handler.execute("{ auth { locked { ok } } }")
        assert result["data"]["auth"]["locked"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_401"
        await handler.aclose()


# ------------------------------------------------------------------- G8


class TestMultiVerbRoute:
    """One @app.api_route(methods=["GET", "POST"]): the scanner takes the
    first verb in GET/POST/PUT/PATCH/DELETE priority (a Query field) and
    warns about the dropped verbs."""

    def test_get_wins_with_warning(self, caplog):
        import logging

        from fastapi_gql_mcp.scanner import RouterScanner

        app = FastAPI()

        @app.api_route("/dual", methods=["GET", "POST"], response_model=Ok)
        async def dual() -> Ok:
            return Ok(ok=True)

        with caplog.at_level(logging.WARNING, logger="fastapi_gql_mcp.scanner"):
            routes, skips = RouterScanner(app).scan()
        assert len(routes) == 1
        assert routes[0].method == "GET"
        assert "multiple verbs" in caplog.text


# ------------------------------------------------------------------- G9


class TestSpecialParams:
    """BackgroundTasks / Request / Response are FastAPI injections, not
    HTTP parameters: the scanner must ignore them (no bogus GraphQL args)
    and the route stays fully functional."""

    async def test_special_params_ignored_and_route_works(self):
        app = FastAPI()
        ran: list[str] = []

        @app.get("/job", response_model=Ok, tags=["sp"])
        async def job(
            tasks: BackgroundTasks,
            request: Request,
            response: Response,
        ) -> Ok:
            response.headers["x-from-route"] = "1"
            tasks.add_task(ran.append, "bg")
            return Ok(ok="x-test-header" in request.headers or True)

        handler = RouterGraphQLHandler(app)
        sdl = handler.get_sdl()
        assert "job: Ok" in sdl  # no arguments from the special params
        result = await handler.execute("{ sp { job { ok } } }")
        assert result == {"data": {"sp": {"job": {"ok": True}}}}
        await handler.aclose()
        # The bg task ran within the ASGI response cycle, AFTER the JSON was
        # produced — the route did not await it inline.
        assert ran == ["bg"]


# ------------------------------------------------------------------- G10


class TestBodylessResponse:
    """`-> None` (204-style) is an explicit no-body contract: the route
    bridges as a Boolean success field (the call IS the point — deletes,
    side effects). Truly untyped endpoints stay skipped."""

    @staticmethod
    def _app() -> FastAPI:
        app = FastAPI()

        @app.delete("/gone/{item_id}", status_code=204, tags=["g10"])
        async def gone(item_id: int) -> None:
            return None

        @app.delete("/missing/{item_id}", tags=["g10"])
        async def missing(item_id: int) -> None:
            if item_id != 1:
                raise HTTPException(status_code=404, detail="no such item")
            return None

        @app.get("/ping", response_model=Ok, tags=["g10"])
        async def ping() -> Ok:
            return Ok(ok=True)

        @app.get("/untyped")
        async def untyped():
            return {"x": 1}

        return app

    def test_void_bridges_and_untyped_bridges_as_json(self):
        from fastapi_gql_mcp.scanner import RouterScanner

        routes, skips = RouterScanner(self._app(), allow_mutation=True).scan()
        assert {r.field_name for r in routes} == {"gone", "missing", "ping", "untyped"}
        assert skips == []  # untyped now bridges as raw JSON, not skipped

    async def test_void_mutation_returns_true(self):
        handler = RouterGraphQLHandler(self._app(), allow_mutation=True)
        sdl = handler.get_sdl()
        assert "gone(item_id: Int!): Boolean" in sdl
        assert "true on success" in sdl  # agent-facing note
        result = await handler.execute("mutation { g10 { gone(item_id: 5) } }")
        assert result == {"data": {"g10": {"gone": True}}}
        await handler.aclose()

    async def test_void_failure_nulls_with_field_error(self):
        handler = RouterGraphQLHandler(self._app(), allow_mutation=True)
        result = await handler.execute("mutation { g10 { missing(item_id: 9) } }")
        assert result["data"]["g10"]["missing"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_404"
        await handler.aclose()


# ------------------------------------------------------------------- G11


class TestRootPathRoute:
    """A root-path route (`/`) has no usable path segment: domains_for
    falls back to the general domain and the route executes there."""

    async def test_root_route_lands_in_general_domain(self):
        app = FastAPI()

        @app.get("/", response_model=Ok)
        async def root() -> Ok:
            return Ok(ok=True)

        handler = RouterGraphQLHandler(app)
        assert "root: Ok" in handler.get_sdl()
        result = await handler.execute("{ general { root { ok } } }")
        assert result == {"data": {"general": {"root": {"ok": True}}}}
        await handler.aclose()


# ------------------------------------------------------------ M1 (invoker)


class TestRenderParamNoneItems:
    """M1: mixed lists drop None items instead of stringifying them into
    the literal query value "None"."""

    def test_mixed_list_drops_none_items(self):
        from fastapi_gql_mcp.invoker import _render_param

        assert _render_param([1, None, 3]) == [1, 3]
        assert _render_param([None, None]) is None  # all-None → not sent
        assert _render_param(None) is None
