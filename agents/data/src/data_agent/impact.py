"""장애 영향 분석의 숫자 집계 (DEP-DATA-02).

영향 원천 숫자의 유일한 출처다. 숫자는 코드로만 계산하고 LLM을 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersResult


@dataclass(frozen=True)
class ImpactCounts:
    failed_order_count: int
    affected_customer_count: int
    damage_amount: int  # 정수 원


def aggregate_impact(result: ShopFailedOrdersResult) -> ImpactCounts:
    """실패 주문 수, 영향 고객 수(같은 buyer_id는 한 번), 피해 금액 합계를 계산한다.

    금액이 음수인 주문이 있으면 ValueError를 낸다 (음수 피해 금액은 없다).
    """
    orders = result.orders
    for order in orders:
        if order.amount < 0:
            raise ValueError(f"negative amount: order_id={order.order_id}")
    return ImpactCounts(
        failed_order_count=len(orders),
        affected_customer_count=len({o.buyer_id for o in orders}),
        damage_amount=sum(o.amount for o in orders),
    )
