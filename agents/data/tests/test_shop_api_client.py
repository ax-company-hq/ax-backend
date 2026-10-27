"""shop-api 실제 클라이언트 테스트 (#59). 네트워크 없이 httpx MockTransport로 응답을 흉내 낸다.

응답 본문은 shop-api와 같은 방식(`{data: <계약 모델 model_dump(mode="json")>, meta}`)으로 만든다.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ax_agent_template.llm import MockLLMClient
from ax_contracts.data_access.shop_failed_orders import (
    FailedOrder,
    ShopFailedOrdersQuery,
    ShopFailedOrdersResult,
)
from ax_contracts.data_access.shop_metric_series import MetricPoint, ShopMetricSeriesResult
from ax_contracts.schemas import AgentRequest, RequestMeta, TaskStatus
from ax_contracts.schemas.data import AnomalyDetectionInput
from data_agent import handlers
from data_agent.clients import shop_client
from data_agent.clients.shop_client import (
    DEFAULT_SHOP_API_URL,
    HttpMetricSource,
    HttpShopClient,
    MockShopClient,
    ShopApi,
    ShopUnavailable,
    get_shop_client,
)
from data_agent.impact import aggregate_impact
from data_agent.metrics.anomaly import BASELINE_DAYS
from data_agent.skills.anomaly_detection import detect_anomalies, get_metric_source

pytestmark = pytest.mark.asyncio

W_FROM = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)
W_TO = datetime(2026, 10, 21, 5, 30, tzinfo=UTC)
BASE = "http://shop.test"
Handler = Callable[[httpx.Request], httpx.Response]


def _api(handler: Handler, token: str | None = "mock:data") -> ShopApi:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ShopApi(BASE, token=token, client=client)


def _ok(model) -> httpx.Response:
    return httpx.Response(200, json={"data": model.model_dump(mode="json"), "meta": {}})


def _orders() -> ShopFailedOrdersResult:
    rows = [
        ("ord_1", "buy_0001", 10000, "books"),
        ("ord_2", "buy_0001", 25000, "books"),
        ("ord_3", "buy_0002", 60000, "goods"),
    ]
    return ShopFailedOrdersResult(
        orders=[
            FailedOrder(
                order_id=oid,
                buyer_id=buyer,
                amount=amount,
                category=category,
                failed_at=W_FROM + timedelta(minutes=i + 1),
                reason="PG_TIMEOUT",
            )
            for i, (oid, buyer, amount, category) in enumerate(rows)
        ],
        collected_at=W_TO,
    )


def _query(**kw) -> ShopFailedOrdersQuery:
    return ShopFailedOrdersQuery(from_=W_FROM, to=W_TO, **kw)


# ---------- 실패 주문: 요청·응답 형식 계약 ----------


async def test_failed_orders_request_and_response_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(_orders())

    client = HttpShopClient(_api(handler))
    result = await client.list_failed_orders(_query(category="books", limit=50))

    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/shop/failed-orders"
    assert request.headers["Authorization"] == "Bearer mock:data"
    params = request.url.params
    assert datetime.fromisoformat(params["from"]) == W_FROM
    assert datetime.fromisoformat(params["to"]) == W_TO
    assert params["category"] == "books"
    assert params["limit"] == "50"
    assert isinstance(result, ShopFailedOrdersResult)
    assert [o.order_id for o in result.orders] == ["ord_1", "ord_2", "ord_3"]
    counts = aggregate_impact(result)
    assert (counts.failed_order_count, counts.affected_customer_count) == (3, 2)
    assert counts.damage_amount == 95000


async def test_failed_orders_without_category_omits_param() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok(ShopFailedOrdersResult(orders=[], truncated=True))

    result = await HttpShopClient(_api(handler)).list_failed_orders(_query())
    assert "category" not in seen[0].url.params
    assert result.orders == []
    assert result.truncated is True


# ---------- DATA_MOCK 전환·주소 ----------


async def test_get_shop_client_switches_on_data_mock() -> None:
    assert isinstance(get_shop_client({"DATA_MOCK": "1"}), MockShopClient)
    real = get_shop_client({})
    assert isinstance(real, HttpShopClient)
    assert real.api.base_url == DEFAULT_SHOP_API_URL == "http://shop-api:8010"
    custom = get_shop_client({"AX_SHOP_API_URL": "http://localhost:8010/"})
    assert isinstance(custom, HttpShopClient)
    assert custom.api.base_url == "http://localhost:8010"


async def test_get_metric_source_switches_on_data_mock() -> None:
    source = get_metric_source({"AX_SHOP_API_URL": "http://localhost:8010"})
    assert isinstance(source, HttpMetricSource)
    assert source.api.base_url == "http://localhost:8010"


# ---------- 오류: 지어내지 않고 ShopUnavailable ----------


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (500, "INTERNAL"), (503, "UNAVAILABLE")],
)
async def test_error_status_raises_unavailable(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "x"}})

    with pytest.raises(ShopUnavailable) as caught:
        await HttpShopClient(_api(handler)).list_failed_orders(_query())
    assert caught.value.code == code
    assert str(status) in str(caught.value)


async def test_error_without_json_body_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    with pytest.raises(ShopUnavailable) as caught:
        await HttpShopClient(_api(handler)).list_failed_orders(_query())
    assert caught.value.code == "UNAVAILABLE"


async def test_no_response_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ShopUnavailable, match="shop-api에 연결할 수 없다") as caught:
        await HttpShopClient(_api(handler)).list_failed_orders(_query())
    assert caught.value.code == "UNAVAILABLE"


async def test_contract_mismatch_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"orders": [{"order_id": "ord_1"}]}})

    with pytest.raises(ShopUnavailable) as caught:
        await HttpShopClient(_api(handler)).list_failed_orders(_query())
    assert caught.value.code == "BAD_RESPONSE"


async def test_missing_token_raises_unavailable_without_calling(monkeypatch) -> None:
    for name in ("DATA_TOKEN", "DATA_MOCK", "AGENT_TEMPLATE_MOCK"):
        monkeypatch.delenv(name, raising=False)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok(_orders())

    with pytest.raises(ShopUnavailable) as caught:
        await HttpShopClient(_api(handler, token=None)).list_failed_orders(_query())
    assert caught.value.code == "UNAUTHORIZED"
    assert calls == []


# ---------- 지표 시계열 ----------


def _series_handler(seen: list[httpx.Request], rate: Callable[[datetime], float]) -> Handler:
    """요청 구간 [from, to)의 5분 버킷마다 `rate(ts)` 값을 돌려준다."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = request.url.params
        start = datetime.fromisoformat(params["from"])
        end = datetime.fromisoformat(params["to"])
        ts = start.replace(minute=start.minute - start.minute % 5, second=0, microsecond=0)
        points = []
        while ts < end:
            points.append(MetricPoint(ts=ts, value=rate(ts)))
            ts += timedelta(minutes=5)
        return _ok(ShopMetricSeriesResult(metric=params["metric"], points=points))

    return handler


async def test_metric_series_reads_in_day_chunks() -> None:
    seen: list[httpx.Request] = []
    source = HttpMetricSource(_api(_series_handler(seen, lambda ts: 0.01)))
    start = W_FROM - timedelta(days=BASELINE_DAYS)
    points = await source.fetch("payment_fail_rate", start, W_TO)

    assert len(seen) == BASELINE_DAYS + 1  # 7일 + 30분 → 하루씩 8번
    for request in seen:
        assert request.url.path == "/api/shop/metric-series"
        assert request.headers["Authorization"] == "Bearer mock:data"
        params = request.url.params
        assert (params["metric"], params["bucket"]) == ("payment_fail_rate", "5m")
        span = datetime.fromisoformat(params["to"]) - datetime.fromisoformat(params["from"])
        assert span <= timedelta(days=1)  # shop-api 한도(2016점) 안
    assert len(points) == BASELINE_DAYS * 288 + 6
    assert points[0][0] == start and points[-1][0] == W_TO - timedelta(minutes=5)
    assert [ts for ts, _ in points] == sorted({ts for ts, _ in points})


async def test_metric_series_skips_metric_shop_api_does_not_compute() -> None:
    seen: list[httpx.Request] = []
    source = HttpMetricSource(_api(_series_handler(seen, lambda ts: 1.0)))
    assert await source.fetch("inquiry_count", W_FROM, W_TO) == []
    assert seen == []


async def test_anomaly_detection_reads_payment_fail_rate_from_shop_api() -> None:
    seen: list[httpx.Request] = []
    source = HttpMetricSource(
        _api(_series_handler(seen, lambda ts: 0.25 if W_FROM <= ts < W_TO else 0.02))
    )
    data = AnomalyDetectionInput(window_from=W_FROM, window_to=W_TO, metrics=["payment_fail_rate"])
    result = await detect_anomalies(data, source, MockLLMClient(responses={}))

    verdict = result.output.verdicts[0]
    assert verdict.metric == "payment_fail_rate"
    assert verdict.is_anomaly is True
    assert verdict.observed == 0.25
    assert verdict.baseline == 0.02
    assert result.output.any_anomaly is True
    assert {r.url.params["metric"] for r in seen} == {"payment_fail_rate"}


async def test_metric_series_error_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "UNAUTHORIZED", "message": "x"}})

    with pytest.raises(ShopUnavailable) as caught:
        await HttpMetricSource(_api(handler)).fetch("payment_fail_rate", W_FROM, W_TO)
    assert caught.value.code == "UNAUTHORIZED"


# ---------- 핸들러: 확인할 수 없음 (숫자 0을 내지 않음) ----------


def _request(skill: str, payload: dict) -> AgentRequest:
    meta = RequestMeta(
        context_id="ctx_inc_1",
        token="mock:orchestrator",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    return AgentRequest(meta=meta, skill=skill, input=payload)


@pytest.fixture
def shop_down(monkeypatch):
    """목 모드를 끄고 shop-api가 503을 돌려주게 한다."""
    for name in ("DATA_MOCK", "AGENT_TEMPLATE_MOCK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_TOKEN", "mock:data")
    monkeypatch.setattr(handlers, "get_llm", lambda: MockLLMClient(responses={}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "UNAVAILABLE", "message": "down"}})

    shop_client.use_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    yield
    shop_client.use_client(None)


async def test_impact_handler_reports_unknown_when_shop_api_down(shop_down) -> None:
    window = {"window_from": W_FROM.isoformat(), "window_to": W_TO.isoformat()}
    result = await handlers.impact_analysis(_request("impact_analysis", window))

    assert result.status == TaskStatus.FAILED
    assert result.outcome == "unavailable"
    assert "failed_order_count" not in result.output
    assert "damage_amount" not in result.output
    assert result.output["message"].startswith("확인할 수 없음")
    assert result.evidence.data_summary.startswith("확인할 수 없음")
    assert result.evidence.queries == ["shop_failed_orders"]


async def test_anomaly_handler_reports_unknown_when_shop_api_down(shop_down) -> None:
    window = {"window_from": W_FROM.isoformat(), "window_to": W_TO.isoformat()}
    result = await handlers.anomaly_detection(_request("anomaly_detection", window))

    assert result.status == TaskStatus.FAILED
    assert result.outcome == "unavailable"
    assert "verdicts" not in result.output
    assert "any_anomaly" not in result.output
    assert result.evidence.data_summary.startswith("확인할 수 없음")
