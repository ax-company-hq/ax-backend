"""판매 사이트(shop-api) 실패 주문 조회 클라이언트.

판매 사이트 DB에 직접 쿼리하지 않는다. 실제 클라이언트는 shop-api 조회 API가 나온 뒤 추가한다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Protocol

from ax_contracts.data_access.shop_failed_orders import (
    FailedOrder,
    ShopFailedOrdersQuery,
    ShopFailedOrdersResult,
)
from data_agent import demo_anchor
from data_agent.seed import DEMO_ALIGN_FROM, INCIDENT_FROM, INCIDENT_TO, SEED_FAILED_ORDERS


class ShopClient(Protocol):
    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult: ...


def demo_shift(window_from: datetime) -> timedelta:
    """시드 장애 시작을 데모 anchor로 옮길 거리. `DEMO_ALIGN_FROM` 전 구간은 0(고정 날짜)이다.

    anchor는 프로세스 안에서 기억한다(`demo_anchor`). 처음 정렬 요청의 구간 시작이 anchor가 되고,
    이후 요청(롤백 뒤 구간 등)은 같은 anchor를 쓴다.
    """
    if window_from < DEMO_ALIGN_FROM:
        return timedelta(0)
    return demo_anchor.anchor_for(window_from) - INCIDENT_FROM


def _demo_order(order: FailedOrder, shift: timedelta) -> FailedOrder:
    """장애 구간 안 주문은 장애 길이에 비례해 압축하고, 함정 주문은 상대 위치 그대로 옮긴다."""
    offset = order.failed_at - INCIDENT_FROM
    if INCIDENT_FROM <= order.failed_at < INCIDENT_TO:
        offset = offset * (demo_anchor.incident_length() / (INCIDENT_TO - INCIDENT_FROM))
    return order.model_copy(update={"failed_at": INCIDENT_FROM + shift + offset})


class MockShopClient:
    """시드 데이터를 구간(from~to, 양끝 포함)과 카테고리로 걸러 돌려준다.

    요청 구간이 `DEMO_ALIGN_FROM` 이후면 시드 장애를 데모 anchor로 옮긴다. 장애 주문 6건은
    [anchor, anchor + 장애 길이) 안에, 함정 3건은 anchor 기준 −5분·+45분·+90분에 놓인다.
    """

    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult:
        shift = demo_shift(query.from_)
        seeds = [_demo_order(o, shift) if shift else o for o in SEED_FAILED_ORDERS]
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
