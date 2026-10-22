"""판매 사이트(shop-api) 실패 주문 조회 클라이언트.

판매 사이트 DB에 직접 쿼리하지 않는다. 실제 클라이언트는 shop-api 조회 API가 나온 뒤 추가한다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

from ax_contracts.data_access.shop_failed_orders import (
    ShopFailedOrdersQuery,
    ShopFailedOrdersResult,
)
from data_agent.seed import SEED_FAILED_ORDERS


class ShopClient(Protocol):
    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult: ...


class MockShopClient:
    """시드 데이터를 구간(from~to, 양끝 포함)과 카테고리로 걸러 돌려준다."""

    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult:
        orders = [
            o
            for o in SEED_FAILED_ORDERS
            if o.tenant_id == query.tenant_id
            and query.from_ <= o.failed_at <= query.to
            and (query.category is None or o.category == query.category)
        ]
        return ShopFailedOrdersResult(orders=orders)


def get_shop_client(env: Mapping[str, str] = os.environ) -> ShopClient:
    """`DATA_MOCK=1`이면 목 클라이언트, 아니면 아직 없다."""
    if env.get("DATA_MOCK") == "1":
        return MockShopClient()
    raise NotImplementedError("실제 shop 클라이언트는 shop-api 조회 API가 나온 뒤에 구현한다")
