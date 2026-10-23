"""약속 배송일(`promised_delivery_at`)과 발송 시각(`shipped_at`): 보상 판정의 사실 근거."""

import sqlite3
from datetime import UTC, datetime, timedelta

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


def _order(client: TestClient) -> dict:
    body = {"buyer_id": "buy_0001", "items": [{"product_id": "prd_0001", "qty": 1}]}
    return client.post("/api/shop/orders", json=body).json()["data"]


def _pay(client: TestClient, order: dict, simulate: str | None = None) -> dict:
    url = f"/api/shop/orders/{order['order_id']}/payments"
    return client.post(url, json={"simulate": simulate} if simulate else {}).json()["data"]


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _ship(client: TestClient, order_id: str, body: dict | None = None, headers=ADMIN):  # noqa: ANN001, ANN202
    return client.post(
        f"/api/shop/admin/orders/{order_id}/shipped", json=body or {}, headers=headers
    )


def _deliver(client: TestClient, order_id: str, body: dict | None = None):  # noqa: ANN202
    return client.post(
        f"/api/shop/admin/orders/{order_id}/delivered", json=body or {}, headers=ADMIN
    )


# --- 약속 배송일 ---------------------------------------------------------------


def test_promised_delivery_is_set_when_the_payment_succeeds() -> None:
    client = TestClient(create_app(Db(":memory:")))
    order = _order(client)
    assert order["promised_delivery_at"] is None and order["shipped_at"] is None
    failed = _pay(client, order, "failure")
    assert failed["promised_delivery_at"] is None  # 결제가 실패했으면 약속하지 않는다
    paid = _pay(client, order)
    promised = _stamp(paid["promised_delivery_at"])
    paid_at = _stamp(paid["payments"][-1]["created_at"])
    assert promised - paid_at == timedelta(days=3)
    assert paid["shipped_at"] is None and paid["delivered_at"] is None


def test_the_promised_date_is_kept_by_later_reads_and_cancel_cannot_clear_it() -> None:
    client = TestClient(create_app(Db(":memory:")))
    paid = _pay(client, _order(client))
    again = client.get(f"/api/shop/orders/{paid['order_id']}").json()["data"]
    assert again["promised_delivery_at"] == paid["promised_delivery_at"]
    listed = client.get("/api/shop/orders", params={"buyer_id": "buy_0001"}).json()["data"]
    assert listed[0]["promised_delivery_at"] == paid["promised_delivery_at"]


# --- 발송 시각 -------------------------------------------------------------------


def test_shipping_is_admin_only_and_needs_a_paid_order(client: TestClient) -> None:
    order = _order(client)
    refused = _ship(client, order["order_id"])
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "ORDER_NOT_PAID")
    paid = _pay(client, order)
    url = f"/api/shop/admin/orders/{paid['order_id']}/shipped"
    assert client.post(url, json={}).status_code == 401
    assert _ship(client, paid["order_id"], headers=SUPPORT).status_code == 403
    assert _ship(client, "ord_9999").status_code == 404
    done = _ship(client, paid["order_id"])
    assert done.status_code == 200 and done.json()["data"]["shipped_at"].endswith("Z")


def test_shipped_at_is_utc_kept_on_repeat_and_changes_nothing_else(client: TestClient) -> None:
    paid = _pay(client, _order(client))
    before = len(events.MOCK_PUBLISHED)
    done = _ship(client, paid["order_id"], {"shipped_at": "2026-10-20T09:30:00+09:00"})
    data = done.json()["data"]
    assert data["shipped_at"] == "2026-10-20T00:30:00Z"  # UTC로 저장
    assert data["status"] == "paid" and data["delivered_at"] is None
    assert len(events.MOCK_PUBLISHED) == before  # 이벤트도 없다
    again = _ship(client, paid["order_id"], {"shipped_at": "2026-10-25T00:00:00Z"})
    assert again.json()["data"]["shipped_at"] == "2026-10-20T00:30:00Z"  # 처음 값 유지
    naive = _ship(client, paid["order_id"], {"shipped_at": "2026-10-20T09:30:00"})
    assert naive.status_code == 422  # 시간대 없는 시각은 거부


def test_shipping_and_delivery_times_stay_in_order(client: TestClient) -> None:
    shipped_first = _pay(client, _order(client))
    _ship(client, shipped_first["order_id"], {"shipped_at": "2026-10-20T00:00:00Z"})
    early = _deliver(client, shipped_first["order_id"], {"delivered_at": "2026-10-19T00:00:00Z"})
    assert (early.status_code, early.json()["error"]["code"]) == (409, "INVALID_TIME")
    ok = _deliver(client, shipped_first["order_id"], {"delivered_at": "2026-10-21T00:00:00Z"})
    assert ok.json()["data"]["delivered_at"] == "2026-10-21T00:00:00Z"
    delivered_first = _pay(client, _order(client))
    _deliver(client, delivered_first["order_id"], {"delivered_at": "2026-10-21T00:00:00Z"})
    late = _ship(client, delivered_first["order_id"], {"shipped_at": "2026-10-22T00:00:00Z"})
    assert (late.status_code, late.json()["error"]["code"]) == (409, "INVALID_TIME")
    assert (
        _ship(
            client, delivered_first["order_id"], {"shipped_at": "2026-10-20T00:00:00Z"}
        ).status_code
        == 200
    )


def test_a_late_delivery_can_be_judged_from_the_order_alone(client: TestClient) -> None:
    """고객지원이 '약속한 날보다 늦게 도착했나'를 주문 응답만으로 코드로 판정할 수 있다."""
    paid = _pay(client, _order(client))
    promised = _stamp(paid["promised_delivery_at"])
    late = (promised + timedelta(days=2)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _ship(
        client,
        paid["order_id"],
        {"shipped_at": (promised - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")},
    )
    done = _deliver(client, paid["order_id"], {"delivered_at": late}).json()["data"]
    assert _stamp(done["delivered_at"]) > _stamp(done["promised_delivery_at"])
    assert _stamp(done["shipped_at"]) < _stamp(done["delivered_at"])


def test_existing_database_files_get_the_new_columns(tmp_path) -> None:  # noqa: ANN001
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE orders (order_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL,"
        " status TEXT NOT NULL, total_amount INTEGER NOT NULL, tenant_id TEXT NOT NULL,"
        " context_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
    )
    old.commit()
    old.close()
    db = Db(path)
    with db.tx() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    assert {"promised_delivery_at", "shipped_at", "delivered_at"} <= columns
