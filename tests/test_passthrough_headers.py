"""Per-call header passthrough: whitelist filter, precedence, MCP e2e.

The security model under test: untrusted caller headers are filtered ONCE at
the untrusted boundary (MCP request, and later /graphql), after which
``execute(headers=...)`` carries the caller's credentials — the SINGLE
identity source (no server-side provider). Default whitelist is
("authorization",); an explicitly empty sequence disables forwarding.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel

from fastapi_gql_mcp import RouterGraphQLHandler, RouterMCP, filter_passthrough_headers

TOKENS = {"jwt-alice": "alice", "jwt-bob": "bob", "jwt-service": "service"}

security = HTTPBearer(auto_error=True)


def bearer_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> str:
    if creds.credentials not in TOKENS:
        raise ValueError(f"unknown token {creds.credentials!r}")
    return TOKENS[creds.credentials]


class WhoOut(BaseModel):
    user: str


class ProbeOut(BaseModel):
    user: str
    x_internal_token_seen: bool


class NoteIn(BaseModel):
    text: str


class NoteOut(BaseModel):
    author: str
    text: str


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami", response_model=WhoOut, tags=["iam"])
    async def whoami(user: str = Depends(bearer_user)) -> WhoOut:
        """Echo the caller's identity as resolved from its Bearer token."""
        return WhoOut(user=user)

    @app.get("/probe", response_model=ProbeOut, tags=["iam"])
    async def probe(request: Request, user: str = Depends(bearer_user)) -> ProbeOut:
        """Report whether a smuggled internal header reached the route."""
        return ProbeOut(
            user=user, x_internal_token_seen="x-internal-token" in request.headers
        )

    @app.post("/notes", response_model=NoteOut, tags=["iam"])
    async def create_note(
        payload: NoteIn, user: str = Depends(bearer_user)
    ) -> NoteOut:
        return NoteOut(author=user, text=payload.text)

    return app


WHOAMI_QUERY = "{ iam { whoami { user } } }"


def tool_payload(result) -> dict:
    return json.loads(result.content[0].text)


def asgi_client_factory(app: FastAPI):
    """fastmcp httpx_client_factory hook routed through in-process ASGI."""

    def factory(**kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    return factory


# --------------------------------------------------------------------- filter


class TestFilterPassthroughHeaders:
    def test_keeps_only_whitelisted(self):
        raw = {"Authorization": "Bearer u", "x-internal-token": "s", "accept": "*/*"}
        assert filter_passthrough_headers(raw, ("authorization",)) == {
            "authorization": "Bearer u"
        }

    def test_case_insensitive_both_sides(self):
        raw = {"AUTHORIZATION": "Bearer u"}
        assert filter_passthrough_headers(raw, ("Authorization",)) == {
            "authorization": "Bearer u"
        }

    def test_empty_allowed_drops_everything(self):
        assert filter_passthrough_headers({"authorization": "Bearer u"}, ()) == {}


# -------------------------------------------------------------- handler config


class TestHandlerConfig:
    def test_normalizes_config_lowercase(self):
        handler = RouterGraphQLHandler(build_app(), passthrough_headers=["Authorization"])
        assert handler.passthrough_headers == ("authorization",)

    def test_default_is_authorization_only(self):
        assert RouterGraphQLHandler(build_app()).passthrough_headers == ("authorization",)

    def test_empty_sequence_disables(self):
        assert RouterGraphQLHandler(build_app(), passthrough_headers=[]).passthrough_headers == ()

    def test_blank_entries_dropped(self):
        handler = RouterGraphQLHandler(build_app(), passthrough_headers=["", "  ", "Authorization"])
        assert handler.passthrough_headers == ("authorization",)


# ------------------------------------------------- invoker extra headers


class TestInvokeExtraHeaders:
    async def test_extra_headers_reach_route(self):
        handler = RouterGraphQLHandler(build_app())
        route = next(r for r in handler.routes if r.path == "/whoami")
        result = await handler.invoker.invoke(
            route, {}, extra_headers={"authorization": "Bearer jwt-alice"}
        )
        assert result == {"user": "alice"}

    async def test_extra_headers_none_is_noop(self):
        handler = RouterGraphQLHandler(build_app())
        route = next(r for r in handler.routes if r.path == "/whoami")
        # No headers, no identity: no server-side credential to fall back on.
        from graphql import GraphQLError

        with pytest.raises(GraphQLError) as exc:
            await handler.invoker.invoke(route, {}, extra_headers=None)
        assert exc.value.extensions["code"] == "HTTP_401"


# ------------------------------------------------- execute chain (context_value)


class TestExecuteChain:
    async def test_headers_reach_route(self):
        handler = RouterGraphQLHandler(build_app())
        result = await handler.execute(WHOAMI_QUERY, headers={"authorization": "Bearer jwt-alice"})
        assert result["data"]["iam"]["whoami"] == {"user": "alice"}

    async def test_without_headers_unchanged(self):
        handler = RouterGraphQLHandler(build_app())
        result = await handler.execute(WHOAMI_QUERY)
        assert result["data"]["iam"]["whoami"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_401"

    async def test_concurrent_executions_do_not_cross(self):
        handler = RouterGraphQLHandler(build_app())

        async def as_user(token: str) -> str:
            r = await handler.execute(WHOAMI_QUERY, headers={"authorization": f"Bearer {token}"})
            return r["data"]["iam"]["whoami"]["user"]

        alice, bob = await asyncio.gather(as_user("jwt-alice"), as_user("jwt-bob"))
        assert (alice, bob) == ("alice", "bob")


class TestAuthPassthroughAdversarial:
    """Reddit feedback (r/mcp): pin the auth-passthrough claim adversarially.

    1. Warm the document cache with one caller, then let a SECOND caller's
       token expire: on the shared cache-hit path the expired caller must
       fail authentication while the other still gets only their own data —
       the compile cache must never carry credentials.
    2. One document mixing an allowed endpoint with a forbidden one: assert
       BOTH the surviving data and the field-level error."""

    @staticmethod
    def _app(state: dict) -> FastAPI:
        app = FastAPI()

        @app.get("/whoami", response_model=WhoOut, tags=["iam"])
        async def whoami(
            authorization: str | None = Header(default=None),
        ) -> WhoOut:
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            if token not in state["valid"]:
                raise HTTPException(status_code=401, detail="expired or invalid token")
            return WhoOut(user=state["users"][token])

        @app.get("/secrets", response_model=NoteOut, tags=["iam"])
        async def secrets(
            authorization: str | None = Header(default=None),
        ) -> NoteOut:
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            if token not in state["valid"]:
                raise HTTPException(status_code=401, detail="expired or invalid token")
            if state["users"][token] != "admin":
                raise HTTPException(status_code=403, detail="admin only")
            return NoteOut(author="admin", text="42")

        return app

    @staticmethod
    def _state() -> dict:
        return {
            "valid": {"tok-alice", "tok-admin"},
            "users": {"tok-alice": "alice", "tok-admin": "admin"},
        }

    async def test_cache_hit_with_expired_token_isolates_callers(self):
        state = self._state()
        handler = RouterGraphQLHandler(self._app(state))
        query = "{ iam { whoami { user } } }"

        # Warm the compile cache as alice (token still valid)...
        warm = await handler.execute(query, headers={"authorization": "Bearer tok-alice"})
        assert warm["data"]["iam"]["whoami"] == {"user": "alice"}

        # ...then alice's token expires AFTER the cache was warmed.
        state["valid"].discard("tok-alice")

        # Same DOCUMENT (compile cache HIT for both), different credentials,
        # launched concurrently.
        async def as_token(token: str):
            return await handler.execute(query, headers={"authorization": f"Bearer {token}"})

        alice, admin = await asyncio.gather(as_token("tok-alice"), as_token("tok-admin"))
        assert alice["data"]["iam"]["whoami"] is None
        assert alice["errors"][0]["extensions"]["code"] == "HTTP_401"
        assert admin["data"]["iam"]["whoami"] == {"user": "admin"}
        assert "errors" not in admin
        await handler.aclose()

    async def test_mixed_allowed_and_forbidden_in_one_document(self):
        state = self._state()
        handler = RouterGraphQLHandler(self._app(state))
        result = await handler.execute(
            "{ iam { whoami { user } secrets { text } } }",
            headers={"authorization": "Bearer tok-alice"},  # member, not admin
        )
        # The allowed sibling survives; only the forbidden field nulls itself.
        assert result["data"]["iam"]["whoami"] == {"user": "alice"}
        assert result["data"]["iam"]["secrets"] is None
        assert result["errors"][0]["extensions"]["code"] == "HTTP_403"
        assert len(result["errors"]) == 1
        await handler.aclose()


# --------------------------------------------------------------- MCP e2e


class TestMcpEndToEnd:
    async def test_per_call_authorization_end_to_end(self):
        app = build_app()
        mcp = RouterMCP(app, name="e2e", passthrough_headers=["authorization"])
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool("graphql_query", {"query": WHOAMI_QUERY})
                )
        assert result["data"]["data"]["iam"]["whoami"] == {"user": "alice"}
        await mcp.handler.aclose()

    async def test_disabled_ignores_client_auth(self):
        app = build_app()
        # Default forwards authorization; an explicitly empty sequence opts out.
        mcp = RouterMCP(app, name="e2e", passthrough_headers=[])
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool("graphql_query", {"query": WHOAMI_QUERY})
                )
        # Client credentials do NOT leak through when passthrough is off.
        assert result["data"]["data"]["iam"]["whoami"] is None
        await mcp.handler.aclose()

    async def test_non_whitelisted_header_not_forwarded(self):
        app = build_app()
        mcp = RouterMCP(app, name="e2e", passthrough_headers=["authorization"])
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice", "x-internal-token": "evil"},
            httpx_client_factory=asgi_client_factory(app),
        )
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool(
                        "graphql_query",
                        {"query": "{ iam { probe { user x_internal_token_seen } } }"},
                    )
                )
        probe = result["data"]["data"]["iam"]["probe"]
        assert probe == {"user": "alice", "x_internal_token_seen": False}
        await mcp.handler.aclose()

    async def test_mutation_tool_passthrough(self):
        app = build_app()
        mcp = RouterMCP(
            app,
            name="e2e",
            allow_mutation=True,
            passthrough_headers=["authorization"],
        )
        mcp.mount_to(app, "/mcp")
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"authorization": "Bearer jwt-alice"},
            httpx_client_factory=asgi_client_factory(app),
        )
        mutation = 'mutation { iam { create_note(payload: {text: "hi"}) { author text } } }'
        async with LifespanManager(app):
            async with Client(transport) as client:
                result = tool_payload(
                    await client.call_tool("graphql_mutation", {"mutation": mutation})
                )
        assert result["data"]["data"]["iam"]["create_note"] == {"author": "alice", "text": "hi"}
        await mcp.handler.aclose()

    async def test_in_memory_client_has_no_identity(self):
        mcp = RouterMCP(build_app(), name="e2e", passthrough_headers=["authorization"])
        async with Client(mcp.mcp) as client:
            result = tool_payload(await client.call_tool("graphql_query", {"query": WHOAMI_QUERY}))
        # No HTTP request context: nothing to forward, nothing to fall back
        # on — protected routes answer 401 (single identity source).
        assert result["data"]["data"]["iam"]["whoami"] is None
        assert result["data"]["errors"][0]["extensions"]["code"] == "HTTP_401"
        await mcp.handler.aclose()
