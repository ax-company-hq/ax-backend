"""판매 사이트(shop-api) 조회 클라이언트: 실패 주문과 지표 시계열 (이슈 #59).

판매 사이트 DB에 직접 쿼리하지 않고 shop-api 읽기 전용 API만 부른다
(`contracts/data-access` `shop_failed_orders`·`shop_metric_series`).
- 주소는 `AX_SHOP_API_URL`(없으면 `http://shop-api:8010`)이다.
- 토큰은 support와 같은 방식이다: `Authorization: Bearer <data 토큰>`(`DATA_TOKEN`, 목 모드는
  `mock:data`). 요청자는 shop-api가 토큰에서 꺼낸다.
- 응답 없음·401·403·5xx·형식 오류는 `ShopUnavailable`이다. 숫자를 지어내지 않고(0으로 내지 않고)
  핸들러가 "확인할 수 없음"으로 답한다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol, get_args

import httpx
from pydantic import ValidationError

from ax_agent_template import config
from ax_contracts.data_access.shop_failed_orders import (
    FailedOrder,
    ShopFailedOrdersQuery,
    ShopFailedOrdersResult,
)
from ax_contracts.data_access.shop_metric_series import MetricName, ShopMetricSeriesResult
from data_agent import demo_anchor
from data_agent.seed import DEMO_ALIGN_FROM, INCIDENT_FROM, INCIDENT_TO, SEED_FAILED_ORDERS

DEFAULT_SHOP_API_URL = "http://shop-api:8010"
TIMEOUT_SECONDS = 10.0
SHOP_METRICS: frozenset[str] = frozenset(get_args(MetricName))
# shop-api는 한 번에 5분 버킷 2016점(7일)까지 준다. 기준선 7일 + 구간을 하루씩 나눠 읽는다
SERIES_CHUNK = timedelta(days=1)

Point = tuple[datetime, float]

_client: httpx.AsyncClient | None = None  # 테스트가 `use_client`로 바꿔 끼운다


class ShopUnavailable(Exception):
    """shop-api에서 데이터를 받지 못했다. `code`는 shop-api 오류 코드 또는 UNAVAILABLE."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ShopClient(Protocol):
    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult: ...


def use_client(client: httpx.AsyncClient | None) -> None:
    """HTTP 클라이언트를 바꿔 끼운다 (테스트용). None이면 호출마다 새로 만든다."""
    global _client
    _client = client


def _iso(value: datetime) -> str:
    return value.isoformat()


class ShopApi:
    """shop-api 읽기 호출 공통부: 주소, Bearer 토큰, `{data}` 풀기, 오류 변환."""

    def __init__(
        self,
        base_url: str = DEFAULT_SHOP_API_URL,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    def _headers(self) -> dict[str, str]:
        try:
            token = self._token or config.agent_token("data")
        except RuntimeError as error:  # DATA_TOKEN 없음: 부르지 않고 확인할 수 없음
            raise ShopUnavailable(
                "UNAUTHORIZED", f"data 토큰이 없어 shop-api를 부를 수 없다 ({error})"
            ) from error
        return {"Authorization": f"Bearer {token}"}

    async def get(self, path: str, params: dict[str, Any]) -> Any:
        """`GET /api/shop{path}`의 `data`를 돌려준다. 200이 아니면 `ShopUnavailable`."""
        headers = self._headers()
        client = self._client or _client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            response = await client.get(
                f"{self.base_url}/api/shop{path}", params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ShopUnavailable(
                "UNAVAILABLE", f"shop-api에 연결할 수 없다 ({type(exc).__name__})"
            ) from exc
        finally:
            if client is not self._client and client is not _client:
                await client.aclose()
        if response.status_code != 200:
            raise _error(response)
        try:
            return response.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ShopUnavailable("BAD_RESPONSE", "shop-api 응답에 data가 없다") from exc


def _error(response: httpx.Response) -> ShopUnavailable:
    status = response.status_code
    try:
        code = str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        code = "UNAVAILABLE"
    return ShopUnavailable(code, f"shop-api 응답 {status} ({code})")


class HttpShopClient:
    """`GET /api/shop/failed-orders`로 실패 주문을 읽는다."""

    def __init__(self, api: ShopApi) -> None:
        self.api = api

    async def list_failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult:
        params: dict[str, Any] = {
            "from": _iso(query.from_),
            "to": _iso(query.to),
            "limit": query.limit,
        }
        if query.category is not None:
            params["category"] = query.category
        data = await self.api.get("/failed-orders", params)
        try:
            return ShopFailedOrdersResult.model_validate(data)
        except ValidationError as exc:
            raise ShopUnavailable("BAD_RESPONSE", "실패 주문 응답 형식이 계약과 다르다") from exc


class HttpMetricSource:
    """`GET /api/shop/metric-series`로 5분 버킷 시계열을 읽는다 (`MetricSource` 구현).

    shop-api가 계산하지 않는 지표(`inquiry_count` 등)는 빈 시계열이다. 기준선이 없어 이상으로
    판정되지 않는다(판정 불가).
    """

    def __init__(self, api: ShopApi) -> None:
        self.api = api

    async def fetch(self, metric: str, window_from: datetime, window_to: datetime) -> list[Point]:
        if metric not in SHOP_METRICS:
            return []
        points: dict[datetime, float] = {}
        start = window_from
        while start < window_to:
            end = min(start + SERIES_CHUNK, window_to)
            data = await self.api.get(
                "/metric-series",
                {"metric": metric, "from": _iso(start), "to": _iso(end), "bucket": "5m"},
            )
            try:
                result = ShopMetricSeriesResult.model_validate(data)
            except ValidationError as exc:
                raise ShopUnavailable("BAD_RESPONSE", "지표 응답 형식이 계약과 다르다") from exc
            for point in result.points:
                if window_from <= point.ts < window_to:
                    points[point.ts] = point.value
            start = end
        return sorted(points.items())


def demo_shift(window_from: datetime) -> timedelta:
    """시드 장애 시작을 데모 anchor로 옮길 거리. `DEMO_ALIGN_FROM` 전 구간은 0(고정 날짜)이다.

    데모 anchor는 이 경로(impact_analysis)만 정한다(`demo_anchor.anchor_for`). 구간 시작이 저장된
    anchor와 1분 이내면 같은 사건이라 기존 anchor를, 그 밖에는 새 사건이라 새 anchor를 쓴다.
    """
    if window_from < DEMO_ALIGN_FROM:
        return timedelta(0)
    return demo_anchor.anchor_for(window_from) - INCIDENT_FROM


def _demo_order(order: FailedOrder, shift: timedelta) -> FailedOrder:
    """장애 구간 안 주문은 [허용 오차, 장애 길이) 안으로 비례 압축하고, 함정 주문은 그대로 옮긴다.

    같은 사건 재조회는 구간 시작이 anchor보다 최대 1분(허용 오차) 늦을 수 있다. 장애 주문을 그 뒤에
    두어야 재조회 구간에서도 6건이 모두 들어간다.
    """
    offset = order.failed_at - INCIDENT_FROM
    if INCIDENT_FROM <= order.failed_at < INCIDENT_TO:
        lead = demo_anchor.SAME_INCIDENT_TOLERANCE
        span = demo_anchor.incident_length() - lead
        offset = lead + offset * (span / (INCIDENT_TO - INCIDENT_FROM))
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


def shop_api(env: Mapping[str, str] = os.environ) -> ShopApi:
    """`AX_SHOP_API_URL`(없으면 `http://shop-api:8010`)의 shop-api."""
    return ShopApi(env.get("AX_SHOP_API_URL") or DEFAULT_SHOP_API_URL)


def get_shop_client(env: Mapping[str, str] = os.environ) -> ShopClient:
    """`DATA_MOCK=1`이면 목 클라이언트, 아니면 shop-api 클라이언트."""
    if env.get("DATA_MOCK") == "1":
        return MockShopClient()
    return HttpShopClient(shop_api(env))
