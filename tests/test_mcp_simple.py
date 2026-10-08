"""MCP simple mode: in-memory fastmcp Client end-to-end."""

import pytest
from fastapi import FastAPI, HTTPException
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
    """Auth-free app: query/composition behavior lives here, credential
    behavior (passthrough, 401s) in test_passthrough_headers.py."""
    app = FastAPI()
    users = {1: UserOut(id=1, name="alice", email="a@x.io"), 2: UserOut(id=2, name="bob")}
    next_id = 3

    @app.get("/users", response_model=list[UserOut], tags=["iam"])
    async def list_users(active: bool = True):
        return list(users.values()) if active else []

    @app.get("/users/{user_id}", response_model=UserOut, tags=["iam"])
    async def get_user(user_id: int):
        if user_id not in users:
            raise HTTPException(status_code=404, detail="no such user")
        return users[user_id]

    @app.post("/users", response_model=UserOut, tags=["iam"])
    async def create_user(payload: UserCreate):
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


class TestGetSchema:
    async def test_get_schema_returns_sdl(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(await client.call_tool("get_schema", {}))
        assert result["success"] is True
        assert "type Query {" in result["data"]["sdl"]
        assert "get_user(user_id: Int!): UserOut" in result["data"]["sdl"]
        assert result["hint"]


class TestGraphqlQuery:
    async def test_query_variables(self, mcp):
        async with Client(mcp.mcp) as client:
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

    async def test_field_projection_and_composition(self, mcp):
        async with Client(mcp.mcp) as client:
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

    async def test_partial_failure_keeps_siblings(self, mcp):
        async with Client(mcp.mcp) as client:
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

    async def test_invalid_query_error_envelope(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool("graphql_query", {"query": "{ nonsense }"})
            )
        assert result["success"] is False
        assert result["error_type"] == "query_execution_error"
        assert "Cannot query field" in result["error"]
        assert result["hint"]

    async def test_query_rejects_mutation_document(self, mcp):
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    {"query": 'mutation { iam { create_user(payload: {name: "x"}) { id } } }'},
                )
            )
        assert result["success"] is False


class TestMutationTool:
    async def test_mutation_execution(self, mcp):
        async with Client(mcp.mcp) as client:
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

    async def test_auto_mode_counts_include_router_routes(self):
        """FastAPI >= 0.142 wraps include_router results in _IncludedRouter
        (not APIRoute instances); the threshold decision must follow the
        SCANNED route count, not isinstance(app.routes, APIRoute)."""
        from fastapi import APIRouter

        class Out(BaseModel):
            id: int

        sub = APIRouter()
        for i in range(30):
            def make_handler(n: int):
                async def handler() -> Out:
                    return Out(id=n)

                handler.__name__ = f"thing_{n}"
                return handler

            sub.get(f"/thing{i}", response_model=Out)(make_handler(i))

        app = FastAPI()
        app.include_router(sub, prefix="/things")

        mcp = RouterMCP(app, name="inc", progressive_threshold=25)
        assert len(mcp.handler.routes) == 30
        assert mcp.mode == "progressive"

    async def test_auto_mode_respects_include_filter(self):
        """Route count for the threshold is the count that ENTERS the schema
        (include/exclude applied), not the raw app.route count."""
        big = FastAPI()

        class Out(BaseModel):
            id: int

        for i in range(30):
            def make_handler(n: int):
                async def handler() -> Out:
                    return Out(id=n)

                handler.__name__ = f"thing_{n}"
                return handler

            big.get(f"/thing{i}", response_model=Out)(make_handler(i))

        mcp = RouterMCP(
            big, name="filtered", include=["/thing0", "/thing1", "/thing2"]
        )
        assert len(mcp.handler.routes) == 3
        assert mcp.mode == "simple"

    async def test_domains_registry_built(self, mcp):
        summary = mcp.domains.summary()
        assert summary and summary[0]["name"] == "iam"


class TestAuthProviderPassthrough:
    def test_auth_provider_reaches_fastmcp(self):
        from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

        provider = InMemoryOAuthProvider(base_url="http://t")
        mcp = RouterMCP(build_app(), name="auth-api", auth=provider)
        assert mcp.mcp.auth is provider

    def test_no_auth_by_default(self, mcp):
        assert mcp.mcp.auth is None

    async def test_mount_exposes_well_known_at_host_root(self):
        """The 401 challenge advertises PR metadata at a host-root URL; the
        mount must keep that promise (RFC 9728/8414)."""
        import httpx
        from asgi_lifespan import LifespanManager
        from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

        app = FastAPI()
        mcp = RouterMCP(
            build_app(), name="auth-api", auth=InMemoryOAuthProvider(base_url="http://localhost:9")
        )
        mcp.mount_to(app, "/mcp")

        well_known = [
            r.path
            for r in app.router.routes
            if getattr(r, "path", "").startswith("/.well-known/")
        ]
        assert well_known, "well-known routes must exist at the host root"

        async with LifespanManager(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:9"
            ) as c:
                for path in well_known:
                    r = await c.get(path)
                    assert r.status_code == 200, (path, r.status_code)

    def test_auth_at_root_requires_auth(self):
        mcp = RouterMCP(build_app(), name="no-auth")
        with pytest.raises(Exception, match="auth_at_root requires"):
            mcp.mount_to(FastAPI(), "/mcp", auth_at_root=True)

    async def test_auth_at_root_serves_endpoint_and_oauth_at_root(self):
        """auth_at_root: MCP endpoint at `path`, OAuth/discovery at the host
        root, the sub-app's auth middleware intact, host routes keep
        precedence."""
        import httpx
        from asgi_lifespan import LifespanManager
        from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

        host = FastAPI()

        @host.get("/auth/callback")
        async def host_callback():
            return {"host": "route-wins"}

        mcp = RouterMCP(
            build_app(),
            name="auth-api",
            auth=InMemoryOAuthProvider(base_url="http://localhost:9"),
        )
        mcp.mount_to(host, "/mcp", auth_at_root=True)

        init = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }

        async with LifespanManager(host):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host), base_url="http://localhost:9"
            ) as c:
                base = {
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                }
                r = await c.post("/mcp", headers=base, json=init)
                assert r.status_code == 401, "endpoint protected by the provider"
                assert "error=" not in r.headers["www-authenticate"]
                # present-but-invalid token: only a running auth backend can
                # tell the two 401s apart (RFC 6750 §3.1)
                r = await c.post(
                    "/mcp", headers={**base, "authorization": "Bearer not-a-token"}, json=init
                )
                assert r.status_code == 401
                assert 'error="invalid_token"' in r.headers["www-authenticate"]
                r = await c.get("/authorize")
                assert r.status_code in (400, 302), "authorize reachable at root"
                r = await c.get("/.well-known/oauth-authorization-server")
                assert r.status_code == 200, "discovery metadata at host root"
                r = await c.get("/auth/callback")
                assert r.json() == {"host": "route-wins"}


class TestMountTo:
    async def test_mounted_endpoint_serves_mcp(self):
        import httpx
        from asgi_lifespan import LifespanManager

        host = build_app()
        mcp = RouterMCP(host, name="mounted", include=["/users*"])
        mcp.mount_to(host, "/mcp")
        # Same-app mount must disable the invoker's own lifespan management.
        assert mcp.handler.invoker.manage_lifespan is False

        async with LifespanManager(host):
            transport = httpx.ASGITransport(app=host)
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

        host = build_app()
        mcp = RouterMCP(host, name="mounted2", include=["/users*"])
        mcp.mount_to(host, "/mcp")

        async with LifespanManager(host):
            transport = httpx.ASGITransport(app=host)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get("/users")
        assert response.status_code == 200
        assert response.json()[0]["name"] == "alice"


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


def build_multi_app() -> FastAPI:
    """iam + billing + untagged routes: one app, several MCP scopes."""
    app = FastAPI()
    users = {1: UserOut(id=1, name="alice", email="a@x.io")}

    @app.get("/users", response_model=list[UserOut], tags=["iam:users"])
    async def list_users():
        return list(users.values())

    @app.post("/users", response_model=UserOut, tags=["iam:users"])
    async def create_user(payload: UserCreate):
        return UserOut(id=2, name=payload.name, email=payload.email)

    @app.get("/invoices", response_model=UserOut, tags=["billing:invoices"])
    async def list_invoices():
        return UserOut(id=1, name="inv-1")

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    return app


class TestTagFiltering:
    async def test_get_schema_scoped_to_tags(self):
        mcp = RouterMCP(
            build_multi_app(), name="iam-api", include_tags=["iam:*"],
            allow_mutation=True,
        )
        sdl = mcp.handler.get_sdl()
        assert "list_users" in sdl and "create_user" in sdl
        assert "list_invoices" not in sdl
        assert "ping" not in sdl  # untagged drops under the whitelist
        summary = mcp.domains.summary()
        assert [d["name"] for d in summary] == ["iam"]

    async def test_query_executes_included_route(self):
        mcp = RouterMCP(build_multi_app(), name="iam-api", include_tags=["iam:*"])
        async with Client(mcp.mcp) as client:
            result = payload(
                await client.call_tool(
                    "graphql_query",
                    # tag "iam:users" nests: IamQuery.users.list_users
                    {"query": "{ iam { users { list_users { id name } } } }"},
                )
            )
        assert result["data"]["data"]["iam"]["users"]["list_users"] == [
            {"id": 1, "name": "alice"}
        ]

    async def test_mutation_filtered_out_by_tags(self):
        mcp = RouterMCP(
            build_multi_app(), name="billing-api", include_tags=["billing*"],
            allow_mutation=True,
        )
        assert "Mutation" not in mcp.handler.get_sdl()

    async def test_auto_mode_counts_post_tag_filter(self):
        big = FastAPI()

        class Out(BaseModel):
            id: int

        for i in range(30):
            tagged = i < 26
            name = f"{'misc' if tagged else 'core'}{i}"
            tags = ["misc"] if tagged else ["core"]

            def make_handler(n: int):
                async def handler() -> Out:
                    return Out(id=n)

                handler.__name__ = f"handler_{n}"
                return handler

            big.get(f"/{name}", response_model=Out, tags=tags)(make_handler(i))

        mcp = RouterMCP(big, name="core-only", include_tags=["core"])
        assert len(mcp.handler.routes) == 4
        assert mcp.mode == "simple"

    async def test_two_instances_same_app_different_tags(self):
        """The motivating scenario: one app, one MCP deployment per use
        case, each scoped by its own tag set."""
        app = build_multi_app()
        iam = RouterMCP(app, name="iam-api", include_tags=["iam:*"])
        billing = RouterMCP(app, name="billing-api", include_tags=["billing*"])

        async with Client(iam.mcp) as client:
            result = payload(await client.call_tool("get_schema", {}))
            sdl = result["data"]["sdl"]
            assert "list_users" in sdl and "list_invoices" not in sdl

        async with Client(billing.mcp) as client:
            result = payload(await client.call_tool("get_schema", {}))
            sdl = result["data"]["sdl"]
            assert "list_invoices" in sdl and "list_users" not in sdl

    async def test_two_mounts_on_one_app(self):
        """Both deployments mounted into the app they wrap, each at its own
        path; the composed lifespan (host -> sub1 -> sub2) enters each part
        exactly once."""
        import httpx
        from asgi_lifespan import LifespanManager

        app = build_multi_app()
        iam = RouterMCP(app, name="iam-api", include_tags=["iam:*"])
        billing = RouterMCP(app, name="billing-api", include_tags=["billing*"])
        iam.mount_to(app, "/mcp-iam")
        billing.mount_to(app, "/mcp-billing")
        assert iam.handler.invoker.manage_lifespan is False
        assert billing.handler.invoker.manage_lifespan is False

        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        responses = {}
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                for path in ("/mcp-iam/", "/mcp-billing/"):
                    responses[path] = await client.post(
                        path, json=init,
                        headers={"Accept": "application/json, text/event-stream"},
                    )
        for path, response in responses.items():
            assert response.status_code == 200, path
            assert "text/event-stream" in response.headers["content-type"], path


class TestReadiness:
    async def test_report_scoped_to_the_deployment(self):
        """The checklist reflects what THIS deployment would expose: /ping
        (a raw-JSON bridge) is tag-filtered out and never reported."""
        mcp = RouterMCP(build_multi_app(), name="iam-api", include_tags=["iam:*"])
        report = mcp.handler.readiness()
        assert [s.path for s in report.skips] == ["/users"]  # POST, mutation off
        assert report.skips[0].tags == ("iam:users",)
        assert report.degraded == ()
        assert report.degraded_fields == ()
        assert not report.ready
