"""The routerql demo app: a small shop with users, catalog, orders and stats.

Feature coverage on purpose:

- tag domains: ``shop:catalog`` / ``shop:orders`` / ``iam:users`` / ``analytics``
- Query Parameter Model (``Annotated[ProductFilter, Query()]``)
- auth via ``x-token`` header (orders + writes need ``demo-secret``)
- collection/item route pairs with distinct endpoint function names
- **descriptions everywhere** — model docstrings, ``Field(description=...)``,
  endpoint docstrings, ``Query()/Path()/Body(description=...)`` all flow into
  the GraphQL schema (hover in GraphiQL to see them)
- one untyped route (``/now``) to demonstrate the skip warning
- lifespan startup log (proves lifespan wiring)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field

DEMO_TOKEN = "demo-secret"


# --------------------------------------------------------------------- schemas


class UserOut(BaseModel):
    """An account holder of the shop."""

    id: int = Field(description="stable user identifier")
    name: str = Field(description="display name")
    email: str = Field(description="contact address")
    role: Literal["admin", "member"] = Field(
        default="member", description="permission group"
    )


class UserCreate(BaseModel):
    """Payload for registering a user."""

    name: str = Field(description="display name")
    email: str = Field(description="contact address")
    role: Literal["admin", "member"] = Field(
        default="member", description="permission group"
    )


class UserFilter(BaseModel):
    """Filters for browsing users (Query Parameter Model)."""

    role: Literal["admin", "member"] | None = Field(
        default=None, description="only users with this role"
    )
    limit: int = Field(default=10, ge=1, le=100, description="max users returned")


class ProductOut(BaseModel):
    """A catalog product as exposed to customers."""

    id: int = Field(description="stable product identifier")
    name: str = Field(description="human-readable product name")
    category: str = Field(description="merchandising category, e.g. coffee")
    price_cents: int = Field(description="unit price in minor currency units")
    in_stock: bool = Field(description="available for purchase right now")


class ProductCreate(BaseModel):
    """Payload for adding a product to the catalog."""

    name: str = Field(description="human-readable product name")
    category: str = Field(description="merchandising category, e.g. coffee")
    price_cents: int = Field(description="unit price in minor currency units")


class ProductFilter(BaseModel):
    """Filters for browsing the catalog (Query Parameter Model)."""

    category: str | None = Field(default=None, description="exact category match")
    in_stock: bool | None = Field(default=None, description="stock availability")
    limit: int = Field(default=10, ge=1, le=100, description="max products returned")


class OrderOut(BaseModel):
    """A purchase order connecting a user to a product."""

    id: int = Field(description="stable order identifier")
    user_id: int = Field(description="the buying user")
    product_id: int = Field(description="the purchased product")
    quantity: int = Field(description="units purchased")
    status: str = Field(description="lifecycle state: created / paid / shipped")


class OrderCreate(BaseModel):
    """Payload for placing an order."""

    product_id: int = Field(description="product to purchase")
    quantity: int = Field(default=1, ge=1, description="units to purchase")


class ShopStats(BaseModel):
    """Aggregated shop health metrics."""

    total_orders: int = Field(description="orders placed, all statuses")
    revenue_cents: int = Field(description="sum of quantity * unit price")
    products_in_stock: int = Field(description="products currently purchasable")


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

    app = FastAPI(title="routerql demo", version="0.3.0", lifespan=lifespan)
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
    async def list_users(
        filters: Annotated[UserFilter, Query(description="how to narrow the user list")],
    ) -> list[UserOut]:
        """Browse shop users.

        Newest users first is NOT guaranteed; use ``role`` + ``limit`` to narrow.
        """
        users = app.state.users.values()
        if filters.role is not None:
            users = (u for u in users if u["role"] == filters.role)
        return [UserOut.model_validate(u) for u in list(users)[: filters.limit]]

    @app.get("/users/{user_id}", response_model=UserOut, tags=["iam:users"])
    async def get_user(
        user_id: Annotated[int, Path(description="the user to fetch")],
    ) -> UserOut:
        """Fetch a single user by id.

        Returns 404-shaped field errors for unknown ids.
        """
        user = app.state.users.get(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        return UserOut.model_validate(user)

    @app.post("/users", response_model=UserOut, tags=["iam:users"])
    async def create_user(
        payload: Annotated[UserCreate, Body(description="the account to register")],
        _token: str = Depends(verify_token),
    ) -> UserOut:
        """Register a user (requires x-token)."""
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
    async def get_product(
        product_id: Annotated[int, Path(description="the product to fetch")],
    ) -> ProductOut:
        """Fetch a single product by id."""
        product = app.state.products.get(product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="product not found")
        return ProductOut.model_validate(product)

    @app.post("/products", response_model=ProductOut, tags=["shop:catalog"])
    async def create_product(
        payload: Annotated[ProductCreate, Body(description="the product to add")],
        _token: str = Depends(verify_token),
    ) -> ProductOut:
        """Add a product to the catalog (requires x-token).

        New products start in stock.
        """
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
    async def get_order(
        order_id: Annotated[int, Path(description="the order to fetch")],
        _token: str = Depends(verify_token),
    ) -> OrderOut:
        """Fetch a single order by id (requires x-token)."""
        order = app.state.orders.get(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return OrderOut.model_validate(order)

    @app.post("/orders", response_model=OrderOut, tags=["shop:orders"])
    async def create_order(
        payload: Annotated[OrderCreate, Body(description="the order to place")],
        _token: str = Depends(verify_token),
    ) -> OrderOut:
        """Place an order (requires x-token).

        Rejects unknown products (400) and out-of-stock ones (409).
        """
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
        """Shop health metrics, computed live over orders and stock."""
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
