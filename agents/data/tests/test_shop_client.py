import asyncio
from datetime import UTC, datetime

import pytest

from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersQuery
from data_agent import seed
from data_agent.clients.shop_client import MockShopClient, get_shop_client
from data_agent.impact import aggregate_impact


def _query(category: str | None = None, **kw) -> ShopFailedOrdersQuery:
    return ShopFailedOrdersQuery(
        from_=kw.get("from_", seed.INCIDENT_FROM),
        to=kw.get("to", seed.INCIDENT_TO),
        category=category,
    )


def test_window_matches_expected_constants() -> None:
    result = asyncio.run(MockShopClient().list_failed_orders(_query()))
    counts = aggregate_impact(result)
    assert counts.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT
    assert counts.affected_customer_count == seed.EXPECTED_AFFECTED_CUSTOMER_COUNT
    assert counts.damage_amount == seed.EXPECTED_DAMAGE_AMOUNT


def test_category_filter() -> None:
    result = asyncio.run(MockShopClient().list_failed_orders(_query("books")))
    assert {o.category for o in result.orders} == {"books"}
    counts = aggregate_impact(result)
    assert counts.failed_order_count == seed.EXPECTED_BOOKS_ORDER_COUNT
    assert counts.affected_customer_count == seed.EXPECTED_BOOKS_CUSTOMER_COUNT
    assert counts.damage_amount == seed.EXPECTED_BOOKS_DAMAGE_AMOUNT


def test_orders_outside_window_excluded() -> None:
    result = asyncio.run(MockShopClient().list_failed_orders(_query()))
    assert all(seed.INCIDENT_FROM <= o.failed_at <= seed.INCIDENT_TO for o in result.orders)
    wide = asyncio.run(
        MockShopClient().list_failed_orders(
            _query(
                from_=datetime(2026, 10, 21, 0, 0, tzinfo=UTC),
                to=datetime(2026, 10, 21, 23, 0, tzinfo=UTC),
            )
        )
    )
    assert len(wide.orders) == len(seed.SEED_FAILED_ORDERS) > len(result.orders)


def test_get_shop_client_mock_flag() -> None:
    assert isinstance(get_shop_client({"DATA_MOCK": "1"}), MockShopClient)


def test_get_shop_client_without_mock_raises() -> None:
    with pytest.raises(NotImplementedError, match="shop-api"):
        get_shop_client({})
