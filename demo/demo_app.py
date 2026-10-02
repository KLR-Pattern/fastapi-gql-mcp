"""A demo FastAPI app: a tiny shop with auth-protected routes.

Run the MCP server over it with ``uv run --extra mcp python -m demo.run_mcp``,
or serve it via ``uv run uvicorn demo.run_http:app --port 8010``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


class ProductOut(BaseModel):
    id: int
    name: str
    price_cents: int
    in_stock: bool = Field(description="available for purchase right now")


class OrderOut(BaseModel):
    id: int
    product_id: int
    quantity: int
    status: str


class OrderCreate(BaseModel):
    product_id: int
    quantity: int = 1


def verify_token(x_token: Annotated[str | None, Header()] = None) -> str:
    if x_token != "demo-secret":
        raise HTTPException(status_code=401, detail="invalid or missing x-token header")
    return x_token


def create_app() -> FastAPI:
    """Build the demo app (fresh per call — tests rely on this)."""
    app = FastAPI(title="routerql demo", version="0.1.0")
    app.state.products = {
        1: {"id": 1, "name": "espresso machine", "price_cents": 12900, "in_stock": True},
        2: {"id": 2, "name": "bur grinder", "price_cents": 8900, "in_stock": False},
        3: {"id": 3, "name": "milk frother", "price_cents": 2400, "in_stock": True},
    }
    app.state.orders = {1: {"id": 1, "product_id": 1, "quantity": 1, "status": "paid"}}
    app.state.next_order_id = 2

    @app.get("/products", response_model=list[ProductOut], tags=["shop:catalog"])
    async def list_products(in_stock: bool | None = None) -> list[ProductOut]:
        items = (ProductOut.model_validate(p) for p in app.state.products.values())
        if in_stock is not None:
            items = (p for p in items if p.in_stock == in_stock)
        return list(items)

    @app.get("/products/{product_id}", response_model=ProductOut, tags=["shop:catalog"])
    async def get_product(product_id: int) -> ProductOut:
        product = app.state.products.get(product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="product not found")
        return ProductOut.model_validate(product)

    @app.get("/orders", response_model=list[OrderOut], tags=["shop:orders"])
    async def list_orders(_token: str = Depends(verify_token)) -> list[OrderOut]:
        return [OrderOut.model_validate(o) for o in app.state.orders.values()]

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
        order = {"id": app.state.next_order_id, **payload.model_dump(), "status": "created"}
        app.state.orders[app.state.next_order_id] = order
        app.state.next_order_id += 1
        return OrderOut.model_validate(order)

    return app


app = create_app()  # module-level instance for uvicorn demo.run_http
