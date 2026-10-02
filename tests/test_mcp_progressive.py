"""MCP progressive mode: 4-layer disclosure over the domain tree."""

import json

import pytest
from fastapi import FastAPI
from fastmcp import Client
from pydantic import BaseModel

from routerql import RouterMCP


class Out(BaseModel):
    id: int
    name: str


class In(BaseModel):
    name: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/users", response_model=list[Out], tags=["iam:users"])
    async def users():
        return [Out(id=1, name="a")]

    @app.get("/roles", response_model=Out, tags=["iam:roles"])
    async def roles():
        return Out(id=1, name="admin")

    @app.get("/invoices", response_model=Out, tags=["billing"])
    async def invoices():
        return Out(id=2, name="inv")

    @app.post("/users", response_model=Out, tags=["iam:users"])
    async def create_user(payload: In):
        return Out(id=9, name=payload.name)

    return app


@pytest.fixture
def mcp():
    return RouterMCP(build_app(), name="prog", allow_mutation=True, mode="progressive")


def payload(result) -> dict:
    return json.loads(result.content[0].text)


class TestListDomains:
    async def test_list_domains(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("list_domains", {}))
        assert result["success"] is True
        domains = {d["name"]: d for d in result["data"]["domains"]}
        assert domains["iam"]["queries"] == 2
        assert domains["iam"]["mutations"] == 1
        assert domains["iam"]["subdomains"] == ["roles", "users"]
        assert domains["billing"]["queries"] == 1


class TestListQueries:
    async def test_subtree_fields_listed(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("list_queries", {"domain": "iam"}))
        names = [q["name"] for q in result["data"]["queries"]]
        assert "get_users" in names and "get_roles" in names
        assert "get_invoices" not in names

    async def test_deep_domain_path(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("list_queries", {"domain": "iam:users"}))
        assert [q["name"] for q in result["data"]["queries"]] == ["get_users"]

    async def test_args_brief(self, mcp):
        app = FastAPI()

        @app.get("/things", response_model=Out, tags=["t"])
        async def things(limit: int = 5):
            return Out(id=1, name="x")

        m = RouterMCP(app, mode="progressive")
        async with Client(m.mcp) as client:
            result = payload(await client.call_tool("list_queries", {"domain": "t"}))
        query = result["data"]["queries"][0]
        assert query["args"] == [{"name": "limit", "type": "Int"}]

    async def test_unknown_domain_error(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("list_queries", {"domain": "nope"}))
        assert result["success"] is False
        assert result["error_type"] == "domain_not_found"
        assert "iam" in result["hint"]

    async def test_list_mutations(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("list_mutations", {"domain": "iam"}))
        assert [m["name"] for m in result["data"]["mutations"]] == ["create_users"]


class TestGetQuerySchema:
    async def test_fragment_scoped_to_domain(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("get_query_schema", {"domain": "iam"}))
        sdl = result["data"]["sdl"]
        assert "get_users" in sdl and "get_roles" in sdl
        assert "get_invoices" not in sdl
        assert "type Out {" in sdl  # reachable shared type included
        assert "type Mutation {" in sdl  # subtree mutation included
        assert "create_users" in sdl

    async def test_leaf_fragment_excludes_siblings(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool("get_query_schema", {"domain": "iam:roles"})
            )
        sdl = result["data"]["sdl"]
        assert "get_roles" in sdl
        assert "get_users" not in sdl
        assert "create_users" not in sdl


class TestExecutionNotScoped:
    async def test_graphql_query_runs_full_schema(self, mcp):
        # Discovery is scoped, execution is not: fields from different domains
        # combine in one query.
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {
                        "query": "{ iam: get_roles { name } billing: get_invoices"
                        " { name } }"
                    },
                )
            )
        data = result["data"]["data"]
        assert data["iam"]["name"] == "admin"
        assert data["billing"]["name"] == "inv"

    async def test_mutation_tool_available(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_mutation",
                    {"mutation": 'mutation { create_users(payload: {name: "n"}) { id } }'},
                )
            )
        assert result["data"]["data"]["create_users"]["id"] == 9
