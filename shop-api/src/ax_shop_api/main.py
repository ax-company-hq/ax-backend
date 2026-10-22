"""ax-shop-api 서버 진입점.

경로는 `/api/shop/<복수 명사>`, 성공은 `{data, meta}`, 실패는 `{error: {code, message}}`다.
PR 1 범위: 상품·주문·가짜 결제·시드·이벤트 발행. 인증은 사람 로그인(JWT)과 에이전트 토큰을 붙일
때(쓰기 API 관문 토큰 등) 추가한다(TODO).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ax_agent_template.config import set_agent_name
from ax_shop_api import outbox
from ax_shop_api.db import Db
from ax_shop_api.seed import seed
from ax_shop_api.service import Shop, ShopError


class ProductIn(BaseModel):
    seller_id: str
    name: str
    category_code: str
    price: int = Field(ge=0)
    stock: int = Field(ge=0)


class ProductPatch(BaseModel):
    name: str | None = None
    price: int | None = Field(default=None, ge=0)
    stock: int | None = Field(default=None, ge=0)
    active: bool | None = None


class OrderLine(BaseModel):
    product_id: str
    qty: int = Field(gt=0)


class OrderIn(BaseModel):
    buyer_id: str
    items: list[OrderLine]


class PaymentIn(BaseModel):
    method_code: str = "card"
    # 가짜 결제 서버에 성공·실패를 요청한다. 장애 주입은 이후 PR에서 게이트웨이가 정한다
    simulate: Literal["success", "failure"] | None = None


class CancelIn(BaseModel):
    reason_code: Literal["buyer", "seller", "system"] = "buyer"


def _ok(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": meta}


def create_app(db: Db | None = None, seed_data: bool = True) -> FastAPI:
    set_agent_name("shop")  # SHOP_MOCK=1로 목 모드(환경변수 접두사 SHOP)
    database = db or Db()
    if seed_data:
        seed(database)
    shop = Shop(database)
    app = FastAPI(title="ax-shop-api")
    app.state.shop = shop

    @app.exception_handler(ShopError)
    async def _shop_error(_: Request, error: ShopError) -> JSONResponse:
        body = {"error": {"code": error.code, "message": error.message}}
        return JSONResponse(status_code=error.status, content=body)

    @app.get("/health")
    def health() -> dict:
        return {"data": {"status": "ok"}, "meta": {}}

    @app.get("/api/shop/sellers")
    async def list_sellers() -> dict:
        return _ok(shop.list_sellers())

    @app.get("/api/shop/products")
    async def list_products(seller_id: str | None = None, category_code: str | None = None) -> dict:
        return _ok(shop.list_products(seller_id, category_code))

    @app.get("/api/shop/products/{product_id}")
    async def get_product(product_id: str) -> dict:
        return _ok(shop.get_product(product_id))

    @app.post("/api/shop/products", status_code=201)
    async def create_product(body: ProductIn) -> dict:
        return _ok(shop.create_product(**body.model_dump()))

    @app.patch("/api/shop/products/{product_id}")
    async def update_product(product_id: str, body: ProductPatch) -> dict:
        return _ok(shop.update_product(product_id, body.model_dump(exclude_none=True)))

    @app.get("/api/shop/orders")
    async def list_orders(buyer_id: str | None = None) -> dict:
        return _ok(shop.list_orders(buyer_id))

    @app.get("/api/shop/orders/{order_id}")
    async def get_order(order_id: str) -> dict:
        return _ok(shop.get_order(order_id))

    @app.post("/api/shop/orders", status_code=201)
    async def create_order(body: OrderIn) -> dict:
        order = shop.create_order(body.buyer_id, [(i.product_id, i.qty) for i in body.items])
        await outbox.flush(database)
        return _ok(order)

    @app.post("/api/shop/orders/{order_id}/payments")
    async def pay(order_id: str, body: PaymentIn) -> dict:
        order = shop.pay(order_id, body.method_code, body.simulate)
        await outbox.flush(database)
        return _ok(order)

    @app.post("/api/shop/orders/{order_id}/cancel")
    async def cancel(order_id: str, body: CancelIn) -> dict:
        order = shop.cancel(order_id, body.reason_code)
        await outbox.flush(database)
        return _ok(order)

    return app


app = create_app()
