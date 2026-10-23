"""ax-shop-api 서버 진입점.

경로는 `/api/shop/<복수 명사>`, 성공은 `{data, meta}`, 실패는 `{error: {code, message}}`다.
PR 1: 상품·주문·가짜 결제·시드·이벤트 발행. PR 2: 장애 주입·해제와 `shop.degraded` 측정.
관리 API(`/api/shop/admin/*`)는 `Authorization: Bearer <토큰>`의 요청자가 허용 목록에 있어야 한다.
사람 로그인(JWT)은 아직 없어서 관리자는 토큰 이름 `admin`으로 대신한다(TODO).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ax_agent_template.config import set_agent_name
from ax_agent_template.server import verify_token
from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersQuery
from ax_contracts.data_access.shop_metric_series import ShopMetricSeriesQuery
from ax_shop_api import outbox
from ax_shop_api.db import Db
from ax_shop_api.lookups import Lookups
from ax_shop_api.promotions import Promotions
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


class FaultIn(BaseModel):
    kind: Literal["latency", "failure"]
    delay_ms: int = Field(default=2000, ge=0, le=60000)  # kind=latency일 때 더할 지연


class BannerIn(BaseModel):
    notice_id: str
    text: str = Field(min_length=1, max_length=200)  # 공개 문구. 화면이 이스케이프해서 보여 준다
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    context_id: str | None = None  # 사후 추적용 사건 번호(선택)


class CouponIn(BaseModel):
    idempotency_key: str = Field(min_length=1)  # `context_id + 액션 + 대상`
    purpose_code: Literal["campaign", "compensation"]
    buyer_id: str
    count: int = Field(gt=0)
    unit_value: int = Field(gt=0)
    campaign_id: str | None = None
    context_id: str | None = None  # 사후 추적용 사건 번호(선택). 멱등 비교에는 쓰지 않는다


# 관리 API 허용 요청자. 장애 해제는 infra 롤백이 관문을 거쳐 부른다(후속 결정 15)
INJECT_ALLOWED = {"admin"}
RELEASE_ALLOWED = {"admin", "infra"}
# 공지·쿠폰 쓰기는 마케팅 관문 토큰만, 쿠폰 조회는 사후 검증·중복 확인을 하는 부서까지
MARKETING_ONLY = {"marketing"}
COUPON_READERS = {"marketing", "support", "finance", "orchestrator"}
# 데이터 조회(읽기 전용)는 분석을 하는 data와 관리자만
DATA_READERS = {"data", "admin"}


def _caller(allowed: set[str]) -> Callable[..., str]:
    def dependency(authorization: str | None = Header(default=None)) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ShopError("UNAUTHORIZED", "토큰이 필요하다", 401)
        try:
            requester = verify_token(token.strip())  # 요청자는 토큰에서 꺼낸다
        except PermissionError as error:
            raise ShopError("UNAUTHORIZED", "토큰을 검증할 수 없다", 401) from error
        if requester not in allowed:
            raise ShopError("FORBIDDEN", "이 API를 호출할 수 없는 요청자다", 403)
        return requester

    return dependency


def _ok(data: Any, **meta: Any) -> dict[str, Any]:
    return {"data": data, "meta": meta}


def _query(model: type[BaseModel], **values: Any) -> Any:
    """쿼리 값을 계약 모델로 검증한다. 시간대가 없는 시각·모르는 지표는 400이다."""
    try:
        return model.model_validate(values)
    except ValueError as error:
        raise ShopError(
            "INVALID_QUERY", f"조회 조건이 올바르지 않다: {error.__class__.__name__}"
        ) from error


def create_app(db: Db | None = None, seed_data: bool = True) -> FastAPI:
    set_agent_name("shop")  # SHOP_MOCK=1로 목 모드(환경변수 접두사 SHOP)
    database = db or Db()
    if seed_data:
        seed(database)
    shop = Shop(database)
    promotions = Promotions(database)
    lookups = Lookups(database)
    interval = float(os.getenv("SHOP_MONITOR_INTERVAL_S") or 5)  # 0이면 주기 판정을 끈다

    async def _monitor_loop() -> None:
        """결제 요청이 없어도 지속 시간을 판정하도록 주기적으로 평가한다."""
        while True:
            await asyncio.sleep(interval)
            shop.evaluate_monitor()
            await outbox.flush(database)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_monitor_loop()) if interval > 0 else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="ax-shop-api", lifespan=lifespan)
    app.state.shop = shop
    app.state.promotions = promotions
    app.state.lookups = lookups

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
        shop.evaluate_monitor()
        await outbox.flush(database)
        return _ok(order)

    @app.post("/api/shop/orders/{order_id}/cancel")
    async def cancel(order_id: str, body: CancelIn) -> dict:
        order = shop.cancel(order_id, body.reason_code)
        await outbox.flush(database)
        return _ok(order)

    @app.get("/api/shop/admin/faults")
    async def fault_state(_: str = Depends(_caller(INJECT_ALLOWED | RELEASE_ALLOWED))) -> dict:
        return _ok(shop.fault_state())

    @app.post("/api/shop/admin/faults", status_code=201)
    async def inject_fault(body: FaultIn, _: str = Depends(_caller(INJECT_ALLOWED))) -> dict:
        return _ok(shop.inject_fault(body.kind, body.delay_ms))

    @app.delete("/api/shop/admin/faults")
    async def release_fault(_: str = Depends(_caller(RELEASE_ALLOWED))) -> dict:
        state = shop.release_fault()
        await outbox.flush(database)
        return _ok(state)

    # --- 공지 배너(SHOP-08): 고객 화면이 읽고, 마케팅 관문 토큰만 게시·해제한다 ---------
    @app.get("/api/shop/banners")
    async def list_banners() -> dict:
        return _ok(promotions.active_banners())

    @app.post("/api/shop/banners", status_code=201)
    async def post_banner(body: BannerIn, _: str = Depends(_caller(MARKETING_ONLY))) -> dict:
        return _ok(
            promotions.post_banner(
                body.notice_id, body.text, body.valid_from, body.valid_to, body.context_id
            )
        )

    @app.delete("/api/shop/banners/{notice_id}")
    async def end_banner(notice_id: str, _: str = Depends(_caller(MARKETING_ONLY))) -> dict:
        return _ok(promotions.end_banner(notice_id))

    # --- 쿠폰(SHOP-10): 발급은 마케팅 관문 토큰만, `idempotency_key` 필수 ---------------
    @app.post("/api/shop/coupons", status_code=201)
    async def issue_coupon(
        body: CouponIn, response: Response, _: str = Depends(_caller(MARKETING_ONLY))
    ) -> dict:
        coupon, replayed = promotions.issue_coupon(**body.model_dump())
        if replayed:
            response.status_code = 200  # 같은 키의 재요청은 이전 쿠폰을 그대로 돌려준다
        return _ok(coupon, replayed=replayed)

    @app.get("/api/shop/coupons")
    async def list_coupons(
        buyer_id: str | None = None,
        purpose_code: str | None = None,
        _: str = Depends(_caller(COUPON_READERS)),
    ) -> dict:
        return _ok(promotions.list_coupons(buyer_id, purpose_code))

    @app.get("/api/shop/coupons/{coupon_id}")
    async def get_coupon(coupon_id: str, _: str = Depends(_caller(COUPON_READERS))) -> dict:
        return _ok(promotions.get_coupon(coupon_id))

    # --- 데이터 조회(contracts/data-access): data 에이전트가 읽는 읽기 전용 API -----------
    @app.get("/api/shop/failed-orders")
    async def failed_orders(
        from_: Annotated[datetime, Query(alias="from")],
        to: datetime,
        category: str | None = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
        _: str = Depends(_caller(DATA_READERS)),
    ) -> dict:
        query = _query(ShopFailedOrdersQuery, from_=from_, to=to, category=category, limit=limit)
        result = lookups.failed_orders(query)
        return _ok(result.model_dump(mode="json"))

    @app.get("/api/shop/metric-series")
    async def metric_series(
        metric: str,
        from_: Annotated[datetime, Query(alias="from")],
        to: datetime,
        bucket: str = "5m",
        _: str = Depends(_caller(DATA_READERS)),
    ) -> dict:
        query = _query(ShopMetricSeriesQuery, metric=metric, from_=from_, to=to, bucket=bucket)
        result = lookups.metric_series(query)
        return _ok(result.model_dump(mode="json"))

    return app


app = create_app()
