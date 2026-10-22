"""장애 영향 분석의 숫자 집계 (DEP-DATA-02).

영향 원천 숫자의 유일한 출처다. 숫자는 코드로만 계산하고 LLM을 쓰지 않는다.
"""

from __future__ import annotations

from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersResult
from pydantic import BaseModel, ConfigDict, Field


class ImpactCounts(BaseModel):
    """집계 결과. 금액은 정수 원이고 모든 값은 0 이상이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failed_orders: int = Field(ge=0)
    affected_customers: int = Field(ge=0)
    lost_amount: int = Field(ge=0)


def aggregate_impact(result: ShopFailedOrdersResult) -> ImpactCounts:
    """실패 주문 목록에서 주문 수, 영향 고객 수(중복 제거), 피해 금액 합계를 계산한다.

    빈 목록이면 모두 0이다. 음수 금액이 있으면 ValueError를 낸다.
    """
    orders = result.orders
    for order in orders:
        if order.amount < 0:
            raise ValueError(f"negative amount: order_id={order.order_id}")
    return ImpactCounts(
        failed_orders=len(orders),
        affected_customers=len({order.buyer_id for order in orders}),
        lost_amount=sum(order.amount for order in orders),
    )
