import pytest
from ax_contracts.data_access.shop_failed_orders import (
    FailedOrder,
    ShopFailedOrdersResult,
)

from data_agent.impact import aggregate_impact


def _order(order_id: str, buyer_id: str, amount: int) -> FailedOrder:
    return FailedOrder.model_validate(
        {
            "order_id": order_id,
            "buyer_id": buyer_id,
            "amount": amount,
            "category": "fashion",
            "failed_at": "2026-01-01T00:00:00Z",
            "reason": "payment_error",
        }
    )


def test_normal() -> None:
    result = ShopFailedOrdersResult(
        orders=[_order("o1", "b1", 10000), _order("o2", "b2", 25000)]
    )
    counts = aggregate_impact(result)
    assert counts.failed_orders == 2
    assert counts.affected_customers == 2
    assert counts.lost_amount == 35000


def test_empty() -> None:
    counts = aggregate_impact(ShopFailedOrdersResult(orders=[]))
    assert (counts.failed_orders, counts.affected_customers, counts.lost_amount) == (
        0,
        0,
        0,
    )


def test_same_customer_counted_once() -> None:
    result = ShopFailedOrdersResult(
        orders=[
            _order("o1", "b1", 1000),
            _order("o2", "b1", 2000),
            _order("o3", "b2", 3000),
        ]
    )
    counts = aggregate_impact(result)
    assert counts.failed_orders == 3
    assert counts.affected_customers == 2
    assert counts.lost_amount == 6000


def test_negative_amount_rejected() -> None:
    with pytest.raises(ValueError):
        aggregate_impact(ShopFailedOrdersResult(orders=[_order("o1", "b1", -1)]))
