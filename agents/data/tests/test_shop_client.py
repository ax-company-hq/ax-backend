import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersQuery
from data_agent import seed
from data_agent.clients import shop_client
from data_agent.clients.shop_client import (
    HttpShopClient,
    MockShopClient,
    ShopUnavailable,
    get_shop_client,
)
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
    """목이 아니면 shop-api 클라이언트다. shop-api가 응답하지 않으면 지어내지 않고 오류를 낸다."""
    client = get_shop_client({})
    assert isinstance(client, HttpShopClient)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    shop_client.use_client(httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    try:
        with pytest.raises(ShopUnavailable, match="shop-api"):
            asyncio.run(client.list_failed_orders(_query()))
    finally:
        shop_client.use_client(None)
