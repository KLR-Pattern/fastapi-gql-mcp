"""GraphiQL + GraphQL-over-HTTP endpoints."""

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from pydantic import BaseModel

from routerql import RouterGraphQLHandler


class Out(BaseModel):
    id: int
    name: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/things", response_model=list[Out])
    async def things():
        return [Out(id=1, name="a")]

    return app


@pytest.fixture
def mounted():
    app = build_app()
    handler = RouterGraphQLHandler(app)
    handler.mount_graphql(app)
    return app, handler


class TestGraphiQLPage:
    async def test_html_served_and_wired(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.get("/graphiql")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "createGraphiQLFetcher({ url: '/graphql' })" in response.text

    async def test_custom_paths(self):
        app = build_app()
        handler = RouterGraphQLHandler(app)
        handler.mount_graphql(app, graphql_path="/gql", graphiql_path="/play")
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                html = await c.get("/play")
                result = await c.post("/gql", json={"query": "{ get_things { id } }"})
        assert "url: '/gql'" in html.text
        assert result.json() == {"data": {"get_things": [{"id": 1}]}}


class TestGraphqlHttp:
    async def test_post_executes_query(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post(
                    "/graphql", json={"query": "{ get_things { id name } }"}
                )
        assert response.status_code == 200
        assert response.json() == {"data": {"get_things": [{"id": 1, "name": "a"}]}}

    async def test_post_with_variables(self, mounted):
        app, _ = mounted
        new = FastAPI()

        @new.get("/items/{item_id}", response_model=Out)
        async def item(item_id: int):
            return Out(id=item_id, name="x")

        handler = RouterGraphQLHandler(new)
        handler.mount_graphql(new)
        async with LifespanManager(new):
            transport = httpx.ASGITransport(app=new)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post(
                    "/graphql",
                    json={
                        "query": "query($id: Int!) { get_items_by_item_id(item_id: $id)"
                        " { name } }",
                        "variables": {"id": 7},
                    },
                )
        assert response.json() == {"data": {"get_items_by_item_id": {"name": "x"}}}

    async def test_validation_error_is_400(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post("/graphql", json={"query": "{ nope }"})
        assert response.status_code == 400
        assert "errors" in response.json()

    async def test_missing_query_is_400(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.post("/graphql", json={})
                bad_json = await c.post(
                    "/graphql", content=b"not-json", headers={"content-type": "application/json"}
                )
        assert response.status_code == 400
        assert bad_json.status_code == 400

    async def test_get_usage_hint(self, mounted):
        app, _ = mounted
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                response = await c.get("/graphql")
        assert response.status_code == 405
        assert response.json()["usage"]["playground"] == "/graphiql"
