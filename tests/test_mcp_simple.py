"""MCP simple mode: in-memory fastmcp Client end-to-end."""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastmcp import Client
from pydantic import BaseModel

from fastapi_gql_mcp import RouterMCP


class UserOut(BaseModel):
    id: int
    name: str
    email: str | None = None


class UserCreate(BaseModel):
    name: str
    email: str | None = None


def build_app() -> FastAPI:
    app = FastAPI()
    users = {1: UserOut(id=1, name="alice", email="a@x.io"), 2: UserOut(id=2, name="bob")}
    next_id = 3

    def auth(x_token: Annotated[str | None, Header()] = None):
        if x_token != "secret":
            raise HTTPException(status_code=401, detail="unauthorized")
        return x_token

    @app.get("/users", response_model=list[UserOut], tags=["iam"])
    async def list_users(active: bool = True, user=Depends(auth)):
        return list(users.values()) if active else []

    @app.get("/users/{user_id}", response_model=UserOut, tags=["iam"])
    async def get_user(user_id: int, user=Depends(auth)):
        if user_id not in users:
            raise HTTPException(status_code=404, detail="no such user")
        return users[user_id]

    @app.post("/users", response_model=UserOut, tags=["iam"])
    async def create_user(payload: UserCreate, user=Depends(auth)):
        nonlocal next_id
        created = UserOut(id=next_id, name=payload.name, email=payload.email)
        users[next_id] = created
        next_id += 1
        return created

    return app


def payload(result) -> dict:
    import json

    return json.loads(result.content[0].text)


@pytest.fixture
def mcp():
    return RouterMCP(build_app(), name="test-api", allow_mutation=True)


@pytest.fixture
def authed_mcp():
    return RouterMCP(
        build_app(),
        name="test-api",
        allow_mutation=True,
        headers_provider=lambda: {"x-token": "secret"},
    )


class TestGetSchema:
    async def test_get_schema_returns_sdl(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("get_schema", {}))
        assert result["success"] is True
        assert "type Query {" in result["data"]["sdl"]
        assert "get_user(user_id: Int!): UserOut" in result["data"]["sdl"]
        assert result["hint"]


class TestGraphqlQuery:
    async def test_query_with_auth_provider(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {"query": "{ iam { list_users { id name } } }"},
                )
            )
        assert result["success"] is True
        data = result["data"]["data"]
        assert data["iam"]["list_users"][0]["name"] == "alice"

    async def test_query_variables(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "query($id: Int!) { iam { get_user(user_id: $id)"
                        " { name email } } }",
                        "variables": {"id": 1},
                    },
                )
            )
        assert result["data"]["data"]["iam"]["get_user"] == {
            "name": "alice",
            "email": "a@x.io",
        }

    async def test_field_projection_and_composition(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ iam { a: list_users(active: false) { id } "
                        "b: get_user(user_id: 2) { name } } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["iam"]["a"] == []
        assert data["iam"]["b"] == {"name": "bob"}

    async def test_partial_failure_keeps_siblings(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ iam { ok: get_user(user_id: 1) { name } "
                        "missing: get_user(user_id: 99) { name } } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["iam"]["ok"] == {"name": "alice"}
        assert data["iam"]["missing"] is None
        assert result["data"]["errors"][0]["extensions"]["code"] == "HTTP_404"

    async def test_without_credentials_401(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool("graphql_query", {"query": "{ iam { list_users { id } } }"})
            )
        # The field failed (401) but the query itself was valid.
        assert result["data"]["data"]["iam"]["list_users"] is None
        assert result["data"]["errors"][0]["extensions"]["code"] == "HTTP_401"

    async def test_invalid_query_error_envelope(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool("graphql_query", {"query": "{ nonsense }"})
            )
        assert result["success"] is False
        assert result["error_type"] == "query_execution_error"
        assert "Cannot query field" in result["error"]
        assert result["hint"]

    async def test_query_rejects_mutation_document(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {"query": 'mutation { iam { create_user(payload: {name: "x"}) { id } } }'},
                )
            )
        assert result["success"] is False


class TestMutationTool:
    async def test_mutation_execution(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_mutation",
                    {
                        "mutation": 'mutation($p: UserCreateInput!) '
                        '{ iam { create_user(payload: $p) { id name } } }',
                        "variables": {"p": {"name": "carol"}},
                    },
                )
            )
        assert result["data"]["data"]["iam"]["create_user"]["name"] == "carol"

    async def test_no_mutation_tool_when_disabled(self):
        mcp = RouterMCP(build_app(), name="ro")
        async with Client(mcp.mcp) as client:
            tools = await client.list_tools()
        assert [t.name for t in tools] == ["get_schema", "graphql_query"]


class TestServerBehavior:
    async def test_auto_mode_small_app_is_simple(self):
        mcp = RouterMCP(build_app(), name="ro")
        assert mcp.mode == "simple"

    async def test_auto_mode_big_app_goes_progressive(self):
        big = FastAPI()

        class Out(BaseModel):
            id: int

        for i in range(30):
            # distinct endpoint function names: duplicate names now fail fast
            def make_handler(n: int):
                async def handler() -> Out:
                    return Out(id=n)

                handler.__name__ = f"thing_{n}"
                return handler

            big.get(f"/thing{i}", response_model=Out)(make_handler(i))

        mcp = RouterMCP(big, name="big", progressive_threshold=25)
        assert mcp.mode == "progressive"
        async with Client(mcp.mcp) as client:
            tools = [t.name for t in await client.list_tools()]
        assert "list_domains" in tools and "graphql_query" in tools
        assert "get_schema" not in tools

    async def test_domains_registry_built(self, mcp):
        summary = mcp.domains.summary()
        assert summary and summary[0]["name"] == "iam"


class TestMountTo:
    async def test_mounted_endpoint_serves_mcp(self):
        import httpx
        from asgi_lifespan import LifespanManager

        from demo.app import create_app

        demo_app = create_app()
        mcp = RouterMCP(demo_app, name="mounted", include=["/products*"])
        mcp.mount_to(demo_app, "/mcp")
        # Same-app mount must disable the invoker's own lifespan management.
        assert mcp.handler.invoker.manage_lifespan is False

        async with LifespanManager(demo_app):
            transport = httpx.ASGITransport(app=demo_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    "/mcp/",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "t", "version": "0"},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

    async def test_host_routes_still_work_after_mount(self):
        import httpx
        from asgi_lifespan import LifespanManager

        from demo.app import create_app

        demo_app = create_app()
        mcp = RouterMCP(demo_app, name="mounted2", include=["/products*"])
        mcp.mount_to(demo_app, "/mcp")

        async with LifespanManager(demo_app):
            transport = httpx.ASGITransport(app=demo_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get("/products")
        assert response.status_code == 200
        assert response.json()[0]["name"] == "espresso machine"


class TestMutationWhitelist:
    async def test_mutation_include_filters_writes(self):
        app = build_app()  # POST /users is the only write route

        mcp = RouterMCP(
            app,
            name="wl",
            allow_mutation=True,
            mutation_include=["/nope*"],
        )
        sdl = mcp.handler.get_sdl()
        assert "Mutation" not in sdl  # no write route survived the whitelist

        mcp2 = RouterMCP(
            app,
            name="wl2",
            allow_mutation=True,
            mutation_include=["/users"],
        )
        assert "create_user" in mcp2.handler.get_sdl()

    async def test_reads_unaffected_by_whitelist(self):
        app = build_app()
        mcp = RouterMCP(app, name="wl3", allow_mutation=True, mutation_include=["/none"])
        sdl = mcp.handler.get_sdl()
        assert "list_users" in sdl
