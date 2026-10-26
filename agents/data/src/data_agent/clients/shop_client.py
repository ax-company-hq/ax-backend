"""판매 사이트(shop-api) 실패 주문 조회 클라이언트.

판매 사이트 DB에 직접 쿼리하지 않는다. 실제 클라이언트는 shop-api 조회 API가 나온 뒤 추가한다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol

from ax_contracts.data_access.shop_failed_orders import (
    ShopFailedOrdersQuery,
    ShopFailedOrdersResult,
)
from data_agent.seed import DEMO_ALIGN_FROM, INCIDENT_FROM, SEED_FAILED_ORDERS


class ShopClient(Protocol):
    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult: ...


def demo_shift(window_from: datetime) -> timedelta:
    """시드를 옮길 거리. 요청 구간이 `DEMO_ALIGN_FROM` 이후에 시작할 때만 옮긴다.

    시연처럼 지금 시각을 물으면 시드 장애 구간 시작을 요청 구간 시작에 맞춘다. 모든 주문을 같은
    거리만큼 옮기므로 구간 밖 함정 주문의 상대 위치도 유지된다. 그 전 구간은 0(고정 날짜)이다.
    """
    if window_from < DEMO_ALIGN_FROM:
        return timedelta(0)
    return window_from - INCIDENT_FROM


class MockShopClient:
    """시드 데이터를 구간(from~to, 양끝 포함)과 카테고리로 걸러 돌려준다.

    요청 구간이 `DEMO_ALIGN_FROM` 이후면 `demo_shift`만큼 옮긴 시드를 쓴다.
    """

    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult:
        shift = demo_shift(query.from_)
        seeds = [
            o.model_copy(update={"failed_at": o.failed_at + shift}) if shift else o
            for o in SEED_FAILED_ORDERS
        ]
        orders = [
            o
            for o in seeds
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
