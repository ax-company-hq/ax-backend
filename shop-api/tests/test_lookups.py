from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_shop_api.db import Db
from ax_shop_api.main import create_app

DATA = {"Authorization": "Bearer mock:data"}
ADMIN = {"Authorization": "Bearer mock:admin"}
FINANCE = {"Authorization": "Bearer mock:finance"}
MARKETING = {"Authorization": "Bearer mock:marketing"}

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)  # 5분 경계


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SHOP_MONITOR_INTERVAL_S", "0")
    events.reset_mock_bus()


@pytest.fixture
def db() -> Db:
    return Db(":memory:")


@pytest.fixture
def client(db: Db) -> TestClient:
    return TestClient(create_app(db))


def _place(
    client: TestClient, db: Db, at: datetime, simulate: str, buyer_id: str = "buy_0001"
) -> str:
    """주문 하나를 만들어 결제하고, 주문·결제 시각을 `at`으로 맞춘다."""
    product = client.get("/api/shop/products").json()["data"][0]
    order = client.post(
        "/api/shop/orders",
        json={"buyer_id": buyer_id, "items": [{"product_id": product["product_id"], "qty": 1}]},
    ).json()["data"]
    client.post(f"/api/shop/orders/{order['order_id']}/payments", json={"simulate": simulate})
    with db.tx() as conn:
        conn.execute(
            "UPDATE orders SET created_at = ? WHERE order_id = ?", (_iso(at), order["order_id"])
        )
        conn.execute(
            "UPDATE payments SET created_at = ? WHERE order_id = ?", (_iso(at), order["order_id"])
        )
    return order["order_id"]


def _range(start: datetime = T0, minutes: int = 30) -> dict:
    return {"from": _iso(start), "to": _iso(start + timedelta(minutes=minutes))}


# --- 권한·입력 -------------------------------------------------------------


@pytest.mark.parametrize("url", ["/api/shop/failed-orders", "/api/shop/metric-series"])
def test_lookups_need_a_data_or_admin_token(client: TestClient, url: str) -> None:
    params = _range() | ({"metric": "order_count"} if "metric" in url else {})
    assert client.get(url, params=params).status_code == 401
    for caller in (FINANCE, MARKETING):
        assert client.get(url, params=params, headers=caller).status_code == 403
    for caller in (DATA, ADMIN):
        assert client.get(url, params=params, headers=caller).status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-10-22T06:00:00", "to": "2026-10-22T06:30:00Z"},  # 시간대 없음
        {"from": "2026-10-22T06:30:00Z", "to": "2026-10-22T06:00:00Z"},  # to가 from보다 앞
    ],
)
def test_bad_range_is_a_400(client: TestClient, params: dict) -> None:
    for url, extra in (
        ("/api/shop/failed-orders", {}),
        ("/api/shop/metric-series", {"metric": "payment_count"}),
    ):
        response = client.get(url, params=params | extra, headers=DATA)
        assert response.status_code == 400
        assert response.json()["error"]["code"] in ("INVALID_QUERY", "INVALID_RANGE")


def test_unknown_metric_and_bucket_are_rejected(client: TestClient) -> None:
    for extra in ({"metric": "revenue"}, {"metric": "order_count", "bucket": "1h"}):
        response = client.get("/api/shop/metric-series", params=_range() | extra, headers=DATA)
        assert response.status_code == 400


# --- 실패 주문 -------------------------------------------------------------


def test_failed_orders_lists_only_failed_payments_in_range(client: TestClient, db: Db) -> None:
    failed = _place(client, db, T0 + timedelta(minutes=1), "failure")
    _place(client, db, T0 + timedelta(minutes=2), "success")
    _place(client, db, T0 + timedelta(minutes=45), "failure")  # 구간 밖
    result = client.get("/api/shop/failed-orders", params=_range(), headers=DATA).json()["data"]
    assert [order["order_id"] for order in result["orders"]] == [failed]
    row = result["orders"][0]
    assert row["buyer_id"] == "buy_0001" and row["reason"] == "PG_DECLINED"
    assert row["failed_at"].startswith("2026-10-22T06:01:00")
    assert row["amount"] > 0 and row["tenant_id"] == "shop-demo"
    assert result["truncated"] is False and result["collected_at"] is not None


def test_failed_orders_filters_by_category_and_marks_truncation(client: TestClient, db: Db) -> None:
    ids = [_place(client, db, T0 + timedelta(minutes=i), "failure") for i in range(3)]
    category = client.get("/api/shop/failed-orders", params=_range(), headers=DATA).json()["data"][
        "orders"
    ][0]["category"]
    other = client.get(
        "/api/shop/failed-orders", params=_range() | {"category": "nope"}, headers=DATA
    ).json()["data"]
    assert other["orders"] == []
    same = client.get(
        "/api/shop/failed-orders", params=_range() | {"category": category}, headers=DATA
    ).json()["data"]
    assert [o["order_id"] for o in same["orders"]] == ids
    cut = client.get(
        "/api/shop/failed-orders", params=_range() | {"limit": 2}, headers=DATA
    ).json()["data"]
    assert [o["order_id"] for o in cut["orders"]] == ids[:2] and cut["truncated"] is True


# --- 지표 시계열 -----------------------------------------------------------


def _series(client: TestClient, metric: str, **extra) -> list[dict]:
    params = _range() | {"metric": metric} | extra
    response = client.get("/api/shop/metric-series", params=params, headers=DATA)
    assert response.status_code == 200
    return response.json()["data"]["points"]


def test_count_metrics_are_bucketed_every_five_minutes_and_zero_filled(
    client: TestClient, db: Db
) -> None:
    _place(client, db, T0 + timedelta(minutes=1), "success")
    _place(client, db, T0 + timedelta(minutes=4), "failure")
    _place(client, db, T0 + timedelta(minutes=11), "failure")
    orders = _series(client, "order_count")
    assert [(p["ts"][11:16], p["value"]) for p in orders] == [
        ("06:00", 2.0), ("06:05", 0.0), ("06:10", 1.0),
        ("06:15", 0.0), ("06:20", 0.0), ("06:25", 0.0),
    ]  # fmt: skip
    assert [p["value"] for p in _series(client, "payment_count")][:3] == [2.0, 0.0, 1.0]
    assert [p["value"] for p in _series(client, "order_fail_count")][:3] == [1.0, 0.0, 1.0]


def test_payment_fail_rate_omits_buckets_without_attempts(client: TestClient, db: Db) -> None:
    _place(client, db, T0 + timedelta(minutes=1), "success")
    _place(client, db, T0 + timedelta(minutes=2), "failure")
    _place(client, db, T0 + timedelta(minutes=3), "failure")
    _place(client, db, T0 + timedelta(minutes=12), "success")
    points = _series(client, "payment_fail_rate")
    assert [(p["ts"][11:16], round(p["value"], 4)) for p in points] == [
        ("06:00", 0.6667),
        ("06:10", 0.0),  # 시도가 있었고 실패가 없으면 진짜 0이다. 시도가 없는 06:05는 없다
    ]


def test_series_start_is_floored_to_the_bucket_and_long_ranges_are_cut(
    client: TestClient,
) -> None:
    middle = {"from": _iso(T0 + timedelta(minutes=2)), "to": _iso(T0 + timedelta(minutes=12))}
    response = client.get(
        "/api/shop/metric-series", params=middle | {"metric": "order_count"}, headers=DATA
    ).json()["data"]
    assert [p["ts"][11:16] for p in response["points"]] == ["06:00", "06:05", "06:10"]
    long = {"from": _iso(T0), "to": _iso(T0 + timedelta(days=9))}
    cut = client.get(
        "/api/shop/metric-series", params=long | {"metric": "order_count"}, headers=DATA
    ).json()["data"]
    assert len(cut["points"]) == 2016 and cut["truncated"] is True
