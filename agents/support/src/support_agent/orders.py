"""주문 조회. 클레임 판정에 쓰는 주문·결제 사실은 이 모듈에서만 읽는다.

주문은 shop-api에서 읽고(`shop_client.load_order`), 목 모드의 시드 주문은 이 모듈이 갖는다.
금액은 정수 원, 시간은 UTC다. 판정 금액은 요청이 아니라 여기 값으로 코드가 계산한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from ax_contracts.schemas.common import AxModel


class OrderItem(AxModel):
    id: str  # 주문 품목 ID (shop-api `order_item_id`)
    title: str
    qty: int
    paid_amount: int  # 이 품목에 실제 낸 현금(포인트 제외)
    points_share: int = 0
    refunded_qty: int = 0


class Order(AxModel):
    id: str  # 주문 ID (shop-api는 `ord_…`)
    order_no: str
    buyer_id: str  # 구매자 ID (shop-api `buyer_id`, 예: buy_0001)
    status: str  # paid | preparing | shipped | delivered
    paid_at: datetime
    promised_delivery_at: datetime | None = None  # 약속한 배송일 (없으면 확인할 수 없음)
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    shipping_fee: int = 0
    points_used: int = 0
    payment_id: str | None = None  # 환불할 결제 (shop-api `payment_id`, 시드는 없음)
    items: list[OrderItem] = Field(default_factory=list)


SEED_ORDERS: list[Order] = [
    Order(
        id="101",
        order_no="ORD-2026-0101",
        buyer_id="buy_0001",
        status="delivered",
        paid_at=datetime(2026, 10, 10, tzinfo=UTC),
        promised_delivery_at=datetime(2026, 10, 12, tzinfo=UTC),
        shipped_at=datetime(2026, 10, 11, tzinfo=UTC),
        delivered_at=datetime(2026, 10, 14, tzinfo=UTC),
        shipping_fee=3000,
        items=[
            OrderItem(id="itm_1", title="고양이의 하루", qty=1, paid_amount=15000),
            OrderItem(id="itm_2", title="탁상 달력", qty=1, paid_amount=15000),
        ],
    ),
    Order(
        id="102",
        order_no="ORD-2026-0102",
        buyer_id="buy_0002",
        status="preparing",
        paid_at=datetime(2026, 10, 21, tzinfo=UTC),
        promised_delivery_at=datetime(2026, 10, 24, tzinfo=UTC),
        shipping_fee=3000,
        points_used=1000,
        items=[
            OrderItem(
                id="itm_3",
                title="고양이 엽서 세트",
                qty=2,
                paid_amount=9000,
                points_share=1000,
            )
        ],
    ),
]


def get_order(order_id: str) -> Order | None:
    return next((o.model_copy(deep=True) for o in SEED_ORDERS if o.id == order_id), None)
