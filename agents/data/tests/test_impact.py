import pytest

from ax_contracts.data_access.shop_failed_orders import (
    FailedOrder,
    ShopFailedOrdersResult,
)
from data_agent.impact import ImpactCounts, aggregate_impact


def _order(order_id: str, buyer_id: str, amount: int) -> FailedOrder:
    return FailedOrder(
        order_id=order_id,
        buyer_id=buyer_id,
        amount=amount,
        category="payment",
        failed_at="2026-01-01T00:00:00Z",
        reason="timeout",
    )


def test_normal() -> None:
    result = ShopFailedOrdersResult(orders=[_order("o1", "b1", 10000), _order("o2", "b2", 25000)])
    assert aggregate_impact(result) == ImpactCounts(2, 2, 35000)


def test_empty() -> None:
    assert aggregate_impact(ShopFailedOrdersResult()) == ImpactCounts(0, 0, 0)


def test_same_customer_counted_once() -> None:
    result = ShopFailedOrdersResult(
        orders=[
            _order("o1", "b1", 1000),
            _order("o2", "b1", 2000),
            _order("o3", "b2", 3000),
        ]
    )
    assert aggregate_impact(result) == ImpactCounts(3, 2, 6000)


def test_negative_amount_rejected() -> None:
    result = ShopFailedOrdersResult(orders=[_order("o1", "b1", -1)])
    with pytest.raises(ValueError):
        aggregate_impact(result)
