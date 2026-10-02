"""MCP simple mode: in-memory fastmcp Client end-to-end."""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastmcp import Client
from pydantic import BaseModel

from routerql import RouterMCP


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
        assert "get_users_by_user_id(user_id: Int!): UserOut" in result["data"]["sdl"]
        assert result["hint"]


class TestGraphqlQuery:
    async def test_query_with_auth_provider(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {"query": "{ get_users { id name } }"},
                )
            )
        assert result["success"] is True
        data = result["data"]["data"]
        assert data["get_users"][0]["name"] == "alice"

    async def test_query_variables(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "query($id: Int!) { get_users_by_user_id(user_id: $id)"
                        " { name email } }",
                        "variables": {"id": 1},
                    },
                )
            )
        assert result["data"]["data"]["get_users_by_user_id"] == {
            "name": "alice",
            "email": "a@x.io",
        }

    async def test_field_projection_and_composition(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ a: get_users(active: false) { id } "
                        "b: get_users_by_user_id(user_id: 2) { name } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["a"] == []
        assert data["b"] == {"name": "bob"}

    async def test_partial_failure_keeps_siblings(self, authed_mcp):
        async with Client(authed_mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ ok: get_users_by_user_id(user_id: 1) { name } "
                        "missing: get_users_by_user_id(user_id: 99) { name } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["ok"] == {"name": "alice"}
        assert data["missing"] is None
        assert result["data"]["errors"][0]["extensions"]["code"] == "HTTP_404"

    async def test_without_credentials_401(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool("graphql_query", {"query": "{ get_users { id } }"})
            )
        # The field failed (401) but the query itself was valid.
        assert result["data"]["data"]["get_users"] is None
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
                    {"query": 'mutation { create_users(payload: {name: "x"}) { id } }'},
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
                        '{ create_users(payload: $p) { id name } }',
                        "variables": {"p": {"name": "carol"}},
                    },
                )
            )
        assert result["data"]["data"]["create_users"]["name"] == "carol"

    async def test_no_mutation_tool_when_disabled(self):
        mcp = RouterMCP(build_app(), name="ro")
        async with Client(mcp.mcp) as client:
            tools = await client.list_tools()
        assert [t.name for t in tools] == ["get_schema", "graphql_query"]


class TestServerBehavior:
    async def test_mode_progressive_not_implemented(self):
        with pytest.raises(NotImplementedError, match="P2"):
            RouterMCP(build_app(), mode="progressive")

    async def test_domains_registry_built(self, mcp):
        summary = mcp.domains.summary()
        assert summary and summary[0]["name"] == "iam"
