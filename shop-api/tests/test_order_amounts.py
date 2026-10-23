"""#119: 주문 응답의 배송비·포인트·품목별 결제액·배송 완료 시각."""

import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_shop_api.db import Db
from ax_shop_api.main import create_app

ADMIN = {"Authorization": "Bearer mock:admin"}
SUPPORT = {"Authorization": "Bearer mock:support"}


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SHOP_MONITOR_INTERVAL_S", "0")
    events.reset_mock_bus()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Db(":memory:")))


def _order(client: TestClient, items: list[tuple[str, int]], points_used: int = 0) -> dict:
    body = {
        "buyer_id": "buy_0001",
        "items": [{"product_id": product, "qty": qty} for product, qty in items],
        "points_used": points_used,
    }
    response = client.post("/api/shop/orders", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _pay(client: TestClient, order: dict) -> dict:
    url = f"/api/shop/orders/{order['order_id']}/payments"
    return client.post(url, json={}).json()["data"]


def _check_sums(order: dict) -> None:
    """결제(현금) 총액 == Σ품목 paid_amount + 배송비, 포인트 합 == points_used."""
    assert (
        order["total_amount"]
        == sum(i["paid_amount"] for i in order["items"]) + order["shipping_fee"]
    )
    assert sum(i["points_share"] for i in order["items"]) == order["points_used"]


def test_small_order_pays_shipping_and_large_order_does_not(client: TestClient) -> None:
    small = _order(client, [("prd_0001", 1)])  # 18,000원 < 50,000원
    assert (small["shipping_fee"], small["total_amount"]) == (3000, 21000)
    just_under = _order(client, [("prd_0001", 1), ("prd_0007", 1)])  # 49,000원 → 배송비
    assert just_under["shipping_fee"] == 3000
    big = _order(client, [("prd_0001", 1), ("prd_0007", 1), ("prd_0004", 1)])  # 56,500원
    assert (big["shipping_fee"], big["total_amount"]) == (0, 56500)
    for order in (small, just_under, big):
        _check_sums(order)


def test_points_reduce_cash_total_and_are_split_by_item_amount(client: TestClient) -> None:
    order = _order(client, [("prd_0001", 1), ("prd_0002", 1)], points_used=4200)  # 18,000 + 24,000
    assert order["points_used"] == 4200 and order["shipping_fee"] == 3000
    shares = [i["points_share"] for i in order["items"]]
    assert shares == [1800, 2400]  # 금액 비율(3:4)
    assert [i["paid_amount"] for i in order["items"]] == [16200, 21600]
    assert order["total_amount"] == 16200 + 21600 + 3000
    _check_sums(order)


def test_points_remainder_is_assigned_so_shares_always_sum_up(client: TestClient) -> None:
    order = _order(client, [("prd_0004", 1), ("prd_0005", 1), ("prd_0003", 1)], points_used=1001)
    assert sum(i["points_share"] for i in order["items"]) == 1001
    assert all(0 <= i["points_share"] <= i["unit_price"] * i["qty"] for i in order["items"])
    _check_sums(order)


def test_points_cannot_exceed_item_total(client: TestClient) -> None:
    body = {"buyer_id": "buy_0001", "items": [{"product_id": "prd_0004", "qty": 1}]}
    response = client.post("/api/shop/orders", json=body | {"points_used": 7501})
    assert (response.status_code, response.json()["error"]["code"]) == (
        400,
        "POINTS_EXCEED_ITEMS",
    )
    assert client.get("/api/shop/orders").json()["data"] == []  # 만들어지지 않았다
    negative = client.post("/api/shop/orders", json=body | {"points_used": -1})
    assert negative.status_code == 422


def test_items_expose_id_title_and_unit_fields(client: TestClient) -> None:
    order = _order(client, [("prd_0001", 2), ("prd_0004", 1)])
    first, second = order["items"]
    assert first["order_item_id"] != second["order_item_id"]
    assert first["order_item_id"].startswith("oit_")
    assert (first["title"], first["qty"], first["unit_price"]) == ("파이썬 입문서", 2, 18000)
    assert (first["paid_amount"], first["points_share"]) == (36000, 0)
    # 이름이 바뀌어도 주문 당시 이름이 남는다
    client.patch("/api/shop/products/prd_0001", json={"name": "바뀐 이름"})
    again = client.get(f"/api/shop/orders/{order['order_id']}").json()["data"]
    assert again["items"][0]["title"] == "파이썬 입문서"


def test_payment_amount_equals_total_and_exposes_created_at(client: TestClient) -> None:
    order = _order(client, [("prd_0001", 1)], points_used=1000)
    paid = _pay(client, order)
    payment = paid["payments"][0]
    assert payment["amount"] == paid["total_amount"] == 18000 - 1000 + 3000
    assert payment["created_at"].endswith("Z") and payment["status"] == "succeeded"
    assert sum(p["amount"] for p in paid["payments"] if p["status"] == "succeeded") == (
        sum(i["paid_amount"] for i in paid["items"]) + paid["shipping_fee"]
    )
    cancelled = client.post(
        f"/api/shop/orders/{_order(client, [('prd_0001', 1)])['order_id']}/cancel", json={}
    )
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_created_event_keeps_its_contract_shape_and_carries_cash_total(
    client: TestClient,
) -> None:
    order = _order(client, [("prd_0001", 1)], points_used=500)
    event = events.MOCK_PUBLISHED[-1]
    assert set(event.data) == {"order_id", "buyer_id", "items", "total_amount"}
    assert set(event.data["items"][0]) == {"product_id", "seller_id", "qty", "unit_price"}
    assert event.data["total_amount"] == order["total_amount"] == 18000 - 500 + 3000


# --- 배송 완료 ------------------------------------------------------------


def _delivered(client: TestClient, order_id: str, headers=ADMIN, body=None):  # noqa: ANN001, ANN202
    return client.post(
        f"/api/shop/admin/orders/{order_id}/delivered", json=body or {}, headers=headers
    )


def test_delivered_at_is_null_until_an_admin_marks_a_paid_order(client: TestClient) -> None:
    order = _pay(client, _order(client, [("prd_0001", 1)]))
    assert order["delivered_at"] is None
    before = len(events.MOCK_PUBLISHED)
    done = _delivered(client, order["order_id"], body={"delivered_at": "2026-10-20T09:30:00+09:00"})
    assert done.status_code == 200
    data = done.json()["data"]
    assert data["delivered_at"] == "2026-10-20T00:30:00Z"  # UTC로 저장
    assert data["status"] == "paid"  # 상태는 그대로
    assert len(events.MOCK_PUBLISHED) == before  # 이벤트도 없다
    again = _delivered(client, order["order_id"], body={"delivered_at": "2026-10-25T00:00:00Z"})
    assert again.json()["data"]["delivered_at"] == "2026-10-20T00:30:00Z"  # 처음 값 유지


def test_delivered_defaults_to_now_and_needs_paid_order(client: TestClient) -> None:
    unpaid = _order(client, [("prd_0001", 1)])
    refused = _delivered(client, unpaid["order_id"])
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "ORDER_NOT_PAID")
    paid = _pay(client, unpaid)
    stamp = _delivered(client, paid["order_id"]).json()["data"]["delivered_at"]
    assert datetime.fromisoformat(stamp.replace("Z", "+00:00")).year == datetime.now(UTC).year
    assert _delivered(client, "ord_9999").status_code == 404


def test_delivered_is_admin_only_and_rejects_naive_time(client: TestClient) -> None:
    paid = _pay(client, _order(client, [("prd_0001", 1)]))
    url = f"/api/shop/admin/orders/{paid['order_id']}/delivered"
    assert client.post(url, json={}).status_code == 401
    assert _delivered(client, paid["order_id"], headers=SUPPORT).status_code == 403
    naive = _delivered(client, paid["order_id"], body={"delivered_at": "2026-10-20T09:30:00"})
    assert naive.status_code == 422


def test_existing_database_files_get_the_new_columns(tmp_path) -> None:  # noqa: ANN001
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE orders (order_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL,"
        " status TEXT NOT NULL, total_amount INTEGER NOT NULL, tenant_id TEXT NOT NULL,"
        " context_id TEXT,"
        " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE order_items (order_id TEXT NOT NULL, product_id TEXT NOT NULL,"
        " seller_id TEXT NOT NULL, qty INTEGER NOT NULL, unit_price INTEGER NOT NULL,"
        " tenant_id TEXT NOT NULL, context_id TEXT);"
    )
    old.commit()
    old.close()
    db = Db(path)
    with db.tx() as conn:
        orders = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        items = {row["name"] for row in conn.execute("PRAGMA table_info(order_items)")}
    assert {"shipping_fee", "points_used", "delivered_at"} <= orders
    assert {"title", "points_share"} <= items
