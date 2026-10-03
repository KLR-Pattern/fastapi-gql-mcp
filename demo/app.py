"""The routerql demo app: a small shop with users, catalog, orders and stats.

Feature coverage on purpose:

- tag domains: ``shop:catalog`` / ``shop:orders`` / ``iam:users`` / ``analytics``
- Query Parameter Model (``Annotated[ProductFilter, Query()]``)
- auth via ``x-token`` header (orders + writes need ``demo-secret``)
- collection/item route pairs with distinct endpoint function names
- one untyped route (``/now``) to demonstrate the skip warning
- lifespan startup log (proves lifespan wiring)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

DEMO_TOKEN = "demo-secret"


# --------------------------------------------------------------------- schemas


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: Literal["admin", "member"] = "member"


class UserCreate(BaseModel):
    name: str
    email: str
    role: Literal["admin", "member"] = "member"


class UserFilter(BaseModel):
    """Query Parameter Model — routerql flattens this into query arguments."""

    role: Literal["admin", "member"] | None = None
    limit: int = Field(default=10, ge=1, le=100)


class ProductOut(BaseModel):
    """A catalog product as exposed to customers."""

    id: int
    name: str
    category: str
    price_cents: int
    in_stock: bool = Field(description="available for purchase right now")


class ProductCreate(BaseModel):
    name: str
    category: str
    price_cents: int


class ProductFilter(BaseModel):
    category: str | None = None
    in_stock: bool | None = None
    limit: int = Field(default=10, ge=1, le=100)


class OrderOut(BaseModel):
    id: int
    user_id: int
    product_id: int
    quantity: int
    status: str


class OrderCreate(BaseModel):
    product_id: int
    quantity: int = 1


class ShopStats(BaseModel):
    total_orders: int
    revenue_cents: int
    products_in_stock: int


# ------------------------------------------------------------------------ auth


def verify_token(x_token: Annotated[str | None, Header()] = None) -> str:
    if x_token != DEMO_TOKEN:
        raise HTTPException(
            status_code=401, detail=f"invalid or missing x-token (use {DEMO_TOKEN=!r})"
        )
    return x_token


# ------------------------------------------------------------------------ app


def create_app() -> FastAPI:
    """Build the demo app (fresh per call — tests rely on this)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.started_at = datetime.now(UTC)
        print("[demo] startup — data is in-memory, mutations live until restart")
        yield
        print("[demo] shutdown")

    app = FastAPI(title="routerql demo", version="0.2.0", lifespan=lifespan)
    app.state.users = {
        1: {"id": 1, "name": "alice", "email": "alice@shop.io", "role": "admin"},
        2: {"id": 2, "name": "bob", "email": "bob@shop.io", "role": "member"},
        3: {"id": 3, "name": "carol", "email": "carol@shop.io", "role": "member"},
    }
    app.state.products = {
        1: {"id": 1, "name": "espresso machine", "category": "coffee",
            "price_cents": 12900, "in_stock": True},
        2: {"id": 2, "name": "bur grinder", "category": "coffee",
            "price_cents": 8900, "in_stock": False},
        3: {"id": 3, "name": "milk frother", "category": "coffee",
            "price_cents": 2400, "in_stock": True},
        4: {"id": 4, "name": "brew scale", "category": "gear",
            "price_cents": 3100, "in_stock": True},
    }
    app.state.orders = {
        1: {"id": 1, "user_id": 1, "product_id": 1, "quantity": 1, "status": "paid"},
        2: {"id": 2, "user_id": 2, "product_id": 3, "quantity": 2, "status": "shipped"},
    }
    app.state.next_id = 100

    def _next(app: FastAPI) -> int:
        app.state.next_id += 1
        return app.state.next_id

    # ------------------------------------------------------------------ users

    @app.get("/users", response_model=list[UserOut], tags=["iam:users"])
    async def list_users(filters: Annotated[UserFilter, Query()]) -> list[UserOut]:
        users = app.state.users.values()
        if filters.role is not None:
            users = (u for u in users if u["role"] == filters.role)
        return [UserOut.model_validate(u) for u in list(users)[: filters.limit]]

    @app.get("/users/{user_id}", response_model=UserOut, tags=["iam:users"])
    async def get_user(user_id: int) -> UserOut:
        user = app.state.users.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        return UserOut.model_validate(user)

    @app.post("/users", response_model=UserOut, tags=["iam:users"])
    async def create_user(payload: UserCreate, _token: str = Depends(verify_token)) -> UserOut:
        user = {"id": _next(app), **payload.model_dump()}
        app.state.users[user["id"]] = user
        return UserOut.model_validate(user)

    # --------------------------------------------------------------- products

    @app.get("/products", response_model=list[ProductOut], tags=["shop:catalog"])
    async def list_products(
        filters: Annotated[ProductFilter, Query(description="catalog filters")],
    ) -> list[ProductOut]:
        """Browse the product catalog.

        Filter by category and stock; results are capped by ``limit``.
        """
        products = app.state.products.values()
        if filters.category is not None:
            products = (p for p in products if p["category"] == filters.category)
        if filters.in_stock is not None:
            products = (p for p in products if p["in_stock"] == filters.in_stock)
        return [ProductOut.model_validate(p) for p in list(products)[: filters.limit]]

    @app.get("/products/{product_id}", response_model=ProductOut, tags=["shop:catalog"])
    async def get_product(product_id: int) -> ProductOut:
        product = app.state.products.get(product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="product not found")
        return ProductOut.model_validate(product)

    @app.post("/products", response_model=ProductOut, tags=["shop:catalog"])
    async def create_product(
        payload: ProductCreate, _token: str = Depends(verify_token)
    ) -> ProductOut:
        product = {"id": _next(app), "in_stock": True, **payload.model_dump()}
        app.state.products[product["id"]] = product
        return ProductOut.model_validate(product)

    # ----------------------------------------------------------------- orders

    @app.get("/orders", response_model=list[OrderOut], tags=["shop:orders"])
    async def list_orders(
        status: Annotated[
            str | None, Query(description="filter by order status, e.g. paid")
        ] = None,
        _token: str = Depends(verify_token),
    ) -> list[OrderOut]:
        """List orders (requires x-token)."""
        orders = app.state.orders.values()
        if status is not None:
            orders = (o for o in orders if o["status"] == status)
        return [OrderOut.model_validate(o) for o in orders]

    @app.get("/orders/{order_id}", response_model=OrderOut, tags=["shop:orders"])
    async def get_order(order_id: int, _token: str = Depends(verify_token)) -> OrderOut:
        order = app.state.orders.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return OrderOut.model_validate(order)

    @app.post("/orders", response_model=OrderOut, tags=["shop:orders"])
    async def create_order(
        payload: OrderCreate, _token: str = Depends(verify_token)
    ) -> OrderOut:
        product = app.state.products.get(payload.product_id)
        if product is None:
            raise HTTPException(status_code=400, detail="unknown product")
        if not product["in_stock"]:
            raise HTTPException(status_code=409, detail="product out of stock")
        order = {"id": _next(app), "user_id": 1, "status": "created", **payload.model_dump()}
        app.state.orders[order["id"]] = order
        return OrderOut.model_validate(order)

    # ------------------------------------------------------------------ stats

    @app.get("/stats", response_model=ShopStats, tags=["analytics"])
    async def shop_stats() -> ShopStats:
        orders = list(app.state.orders.values())
        prices = app.state.products
        return ShopStats(
            total_orders=len(orders),
            revenue_cents=sum(
                o["quantity"] * prices[o["product_id"]]["price_cents"] for o in orders
            ),
            products_in_stock=sum(1 for p in app.state.products.values() if p["in_stock"]),
        )

    # ------------------------------------------------------------ misc / skip

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/now")
    async def now():
        """Untyped on purpose — routerql skips this route with a warning."""
        return {"now": datetime.now(UTC).isoformat()}

    return app


app = create_app()  # module-level instance (tests import create_app instead)
