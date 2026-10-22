import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_contracts.schemas import EventEnvelope, EventType, PaymentFailedData
from ax_shop_api import outbox
from ax_shop_api.db import Db
from ax_shop_api.main import create_app


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    events.reset_mock_bus()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Db(":memory:")))


def _order(client: TestClient, qty: int = 2, product_id: str = "prd_0001") -> dict:
    body = {"buyer_id": "buy_0001", "items": [{"product_id": product_id, "qty": qty}]}
    response = client.post("/api/shop/orders", json=body)
    assert response.status_code == 201
    return response.json()["data"]


def _types() -> list[str]:
    return [e.type.value for e in events.MOCK_PUBLISHED]


def test_seed_has_books_and_stationery(client: TestClient) -> None:
    products = client.get("/api/shop/products").json()["data"]
    assert len(products) >= 8
    assert {p["category_code"] for p in products} == {"book", "stationery"}
    assert len(client.get("/api/shop/sellers").json()["data"]) == 3
    assert (
        client.get("/api/shop/products?seller_id=sel_0002").json()["data"][0]["seller_id"]
        == "sel_0002"
    )


def test_response_envelope_and_error_format(client: TestClient) -> None:
    ok = client.get("/api/shop/products/prd_0001").json()
    assert set(ok) == {"data", "meta"}
    missing = client.get("/api/shop/products/prd_9999")
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "PRODUCT_NOT_FOUND", "message": "상품을 찾을 수 없다"}
    }


def test_order_total_is_computed_from_db_prices(client: TestClient) -> None:
    order = _order(client, qty=3)
    assert order["total_amount"] == 3 * 18000
    assert order["status"] == "pending_payment"
    assert order["items"][0]["seller_id"] == "sel_0001"
    assert client.get("/api/shop/products/prd_0001").json()["data"]["stock"] == 197


def test_order_created_event_follows_catalog_fields(client: TestClient) -> None:
    order = _order(client)
    assert _types() == ["order.created"]
    event = events.MOCK_PUBLISHED[0]
    assert (event.source, event.tenant_id, event.context_id) == ("shop-api", "shop-demo", None)
    assert event.idempotency_key == f"order.created:{order['order_id']}"
    assert set(event.data) == {"order_id", "buyer_id", "items", "total_amount"}
    assert set(event.data["items"][0]) == {"product_id", "seller_id", "qty", "unit_price"}
    EventEnvelope.model_validate(event.model_dump())  # 계약 봉투 형식


def test_payment_success_marks_paid_and_publishes(client: TestClient) -> None:
    order = _order(client)
    paid = client.post(f"/api/shop/orders/{order['order_id']}/payments", json={}).json()["data"]
    assert paid["status"] == "paid" and paid["payments"][0]["status"] == "succeeded"
    event = events.MOCK_PUBLISHED[-1]
    assert event.type == EventType.PAYMENT_SUCCEEDED
    assert event.data["amount"] == order["total_amount"] and event.data["method_code"] == "card"


def test_payment_failure_publishes_payment_failed_and_allows_retry(client: TestClient) -> None:
    order = _order(client)
    url = f"/api/shop/orders/{order['order_id']}/payments"
    failed = client.post(url, json={"simulate": "failure"}).json()["data"]
    assert failed["status"] == "payment_failed"
    event = events.MOCK_PUBLISHED[-1]
    assert event.type == EventType.PAYMENT_FAILED
    assert PaymentFailedData.model_validate(event.data).failure_code == "PG_DECLINED"
    retried = client.post(url, json={}).json()["data"]
    assert retried["status"] == "paid" and len(retried["payments"]) == 2


def test_paid_order_cannot_be_paid_again_or_cancelled(client: TestClient) -> None:
    order = _order(client)
    url = f"/api/shop/orders/{order['order_id']}"
    client.post(f"{url}/payments", json={})
    assert client.post(f"{url}/payments", json={}).json()["error"]["code"] == "ORDER_NOT_PAYABLE"
    cancel = client.post(f"{url}/cancel", json={})
    assert (cancel.status_code, cancel.json()["error"]["code"]) == (409, "ORDER_NOT_CANCELLABLE")


def test_cancel_restores_stock_and_publishes_amount(client: TestClient) -> None:
    order = _order(client, qty=2)
    cancelled = client.post(
        f"/api/shop/orders/{order['order_id']}/cancel", json={"reason_code": "seller"}
    )
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert client.get("/api/shop/products/prd_0001").json()["data"]["stock"] == 200
    event = events.MOCK_PUBLISHED[-1]
    assert event.type == EventType.ORDER_CANCELLED
    assert event.data == {
        "order_id": order["order_id"],
        "reason_code": "seller",
        "cancelled_amount": order["total_amount"],
    }


def test_out_of_stock_and_unknown_buyer_do_not_create_orders(client: TestClient) -> None:
    too_many = {"buyer_id": "buy_0001", "items": [{"product_id": "prd_0001", "qty": 9999}]}
    assert client.post("/api/shop/orders", json=too_many).json()["error"]["code"] == "OUT_OF_STOCK"
    unknown = {"buyer_id": "buy_xxxx", "items": [{"product_id": "prd_0001", "qty": 1}]}
    assert client.post("/api/shop/orders", json=unknown).status_code == 404
    assert client.get("/api/shop/orders").json()["data"] == []
    assert not events.MOCK_PUBLISHED  # 실패한 주문은 이벤트도 남기지 않는다


def test_failed_order_rolls_back_stock(client: TestClient) -> None:
    body = {
        "buyer_id": "buy_0001",
        "items": [{"product_id": "prd_0001", "qty": 1}, {"product_id": "prd_9999", "qty": 1}],
    }
    assert client.post("/api/shop/orders", json=body).status_code == 404
    assert client.get("/api/shop/products/prd_0001").json()["data"]["stock"] == 200


def test_unsent_events_are_kept_and_redelivered_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Db(":memory:")
    app = create_app(db)
    client = TestClient(app)
    real_publish = outbox.publish

    async def not_connected(envelope):  # noqa: ANN001
        raise NotImplementedError("Redis 연동 전")

    # CI는 환경변수로 목 모드를 항상 켜므로, 목 모드 해제 대신 발행 함수를 직접 바꾼다
    monkeypatch.setattr(outbox, "publish", not_connected)
    order = _order(client)
    with db.tx() as conn:
        unsent = conn.execute("SELECT COUNT(*) FROM outbox_events WHERE sent_at IS NULL")
        assert unsent.fetchone()[0] == 1
    monkeypatch.setattr(outbox, "publish", real_publish)
    client.post(
        f"/api/shop/orders/{order['order_id']}/payments", json={}
    )  # flush가 밀린 것까지 보낸다
    assert _types() == ["order.created", "payment.succeeded"]
    assert len({e.event_id for e in events.MOCK_PUBLISHED}) == 2


def test_product_create_and_update(client: TestClient) -> None:
    created = client.post(
        "/api/shop/products",
        json={
            "seller_id": "sel_0001",
            "name": "새 책",
            "category_code": "book",
            "price": 12000,
            "stock": 5,
        },
    )
    assert created.status_code == 201
    product_id = created.json()["data"]["product_id"]
    patched = client.patch(
        f"/api/shop/products/{product_id}", json={"price": 11000, "active": False}
    )
    assert patched.json()["data"]["price"] == 11000
    listed = {p["product_id"] for p in client.get("/api/shop/products").json()["data"]}
    assert product_id not in listed  # 판매 중지된 상품은 목록에서 빠진다
    unknown_seller = {
        "seller_id": "sel_9",
        "name": "x",
        "category_code": "book",
        "price": 1,
        "stock": 1,
    }
    assert client.post("/api/shop/products", json=unknown_seller).status_code == 404
