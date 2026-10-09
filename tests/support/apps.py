"""Shared FastAPI app factories — one canonical app per scenario.

Factories are FUNCTIONS called per test. Several build mutable state
(users_app's dict, multi_domain_app's users) that tests may mutate, so a
module-level app instance would leak state between tests. Never bind one.
"""

from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tests.support.models import (
    ItemCreate,
    ItemOut,
    NoteIn,
    NoteOut,
    ProbeOut,
    ThingOut,
    UserCreate,
    UserOut,
    WhoOut,
)

TOKENS = {"jwt-alice": "alice", "jwt-bob": "bob", "jwt-service": "service"}

security = HTTPBearer(auto_error=True)


def bearer_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> str:
    if creds.credentials not in TOKENS:
        raise ValueError(f"unknown token {creds.credentials!r}")
    return TOKENS[creds.credentials]


def items_app() -> FastAPI:
    """Shop items CRUD: the canonical scan/build/execute fixture app."""
    app = FastAPI()

    @app.get("/items", response_model=list[ItemOut], tags=["shop"])
    async def list_items(limit: int = 2):
        return [ItemOut(id=i, name=f"i{i}") for i in range(limit)]

    @app.get("/items/{item_id}", response_model=ItemOut, tags=["shop"])
    async def get_item(item_id: int):
        return ItemOut(id=item_id, name="single")

    @app.post("/items", response_model=ItemOut, tags=["items"])
    async def create_item(payload: ItemCreate):
        return ItemOut(id=99, name=payload.name)

    return app


def users_app() -> FastAPI:
    """Auth-free iam app: query/composition behavior, no credentials.

    Credential behavior (passthrough, 401s) lives in auth_app instead.
    """
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


def multi_domain_app() -> FastAPI:
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


def auth_app() -> FastAPI:
    """Bearer-token iam app for credential-passthrough scenarios."""
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


def big_app(n: int = 30) -> FastAPI:
    """App with ``n`` distinct GET routes, for progressive-threshold tests.

    Endpoint function names are distinct (``thing_0`` …) because duplicate
    names now fail fast at scan time.
    """
    app = FastAPI()

    for i in range(n):

        def make_handler(k: int):
            async def handler() -> ThingOut:
                return ThingOut(id=k)

            handler.__name__ = f"thing_{k}"
            return handler

        app.get(f"/thing{i}", response_model=ThingOut)(make_handler(i))

    return app


def scanner_app() -> FastAPI:
    """Route-exposure matrix app: one route per scanner outcome (clean,
    dependency-merged, raw Response, hidden, required-header, multi-verb)."""
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


def add_ping(app: FastAPI) -> None:
    """Attach the minimal tagged ping route shared by contract suites."""

    @app.get("/ping", response_model=str, tags=["t"])
    async def ping() -> str:
        return "pong"
