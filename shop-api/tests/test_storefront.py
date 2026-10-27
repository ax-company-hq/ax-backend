"""쇼핑몰 화면용 API: 데모 사용자·카테고리·장바구니 가격 미리보기."""

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_shop_api.db import Db
from ax_shop_api.main import create_app


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SHOP_MONITOR_INTERVAL_S", "0")
    events.reset_mock_bus()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Db(":memory:")))


def test_users_list_is_pseudonymous(client: TestClient) -> None:
    body = client.get("/api/shop/users").json()
    users = body["data"]
    assert len(users) == 30 and body["meta"] == {}
    assert users[0] == {"buyer_id": "buy_0001", "display_name": "데모 구매자 0001"}


def test_select_user_and_unknown_user(client: TestClient) -> None:
    ok = client.post("/api/shop/sessions", json={"buyer_id": "buy_0002"})
    assert ok.status_code == 200 and ok.json()["data"]["user"]["buyer_id"] == "buy_0002"
    assert "token" not in ok.text
    assert client.get("/api/shop/users/buy_0003").json()["data"]["buyer_id"] == "buy_0003"
    missing = client.post("/api/shop/sessions", json={"buyer_id": "buy_9999"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "BUYER_NOT_FOUND"


def test_categories_with_counts(client: TestClient) -> None:
    data = client.get("/api/shop/categories").json()["data"]
    assert data == [
        {"category_code": "book", "name": "도서", "product_count": 4},
        {"category_code": "stationery", "name": "문구", "product_count": 4},
    ]


def test_cart_price_charges_shipping_below_threshold(client: TestClient) -> None:
    body = {"items": [{"product_id": "prd_0001", "qty": 1}]}  # 18,000원
    data = client.post("/api/shop/cart/price", json=body).json()["data"]
    assert data["subtotal"] == 18000 and data["shipping_fee"] == 3000
    assert data["payable_amount"] == 21000
    assert data["coupon_discount"] is None and data["coupon_unknown_reason"]


def test_cart_price_matches_create_order_total(client: TestClient) -> None:
    body = {"items": [{"product_id": "prd_0002", "qty": 2}], "points_used": 1000}  # 48,000원
    price = client.post("/api/shop/cart/price", json=body).json()["data"]
    assert price["shipping_fee"] == 3000 and price["payable_amount"] == 50000
    order = client.post("/api/shop/orders", json={"buyer_id": "buy_0001", **body}).json()["data"]
    assert order["total_amount"] == price["payable_amount"]


def test_cart_price_free_shipping_and_no_side_effects(client: TestClient) -> None:
    body = {"items": [{"product_id": "prd_0007", "qty": 2}]}  # 62,000원
    data = client.post("/api/shop/cart/price", json=body).json()["data"]
    assert data["shipping_fee"] == 0 and data["payable_amount"] == 62000
    assert client.get("/api/shop/products/prd_0007").json()["data"]["stock"] == 90
    assert client.get("/api/shop/orders").json()["data"] == []


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"items": []}, 400, "EMPTY_CART"),
        ({"items": [{"product_id": "prd_9999", "qty": 1}]}, 404, "PRODUCT_NOT_FOUND"),
        ({"items": [{"product_id": "prd_0001", "qty": 9999}]}, 409, "OUT_OF_STOCK"),
        (
            {"items": [{"product_id": "prd_0004", "qty": 1}], "points_used": 8000},
            400,
            "POINTS_EXCEED_ITEMS",
        ),
    ],
)
def test_cart_price_errors(client: TestClient, body: dict, status: int, code: str) -> None:
    res = client.post("/api/shop/cart/price", json=body)
    assert res.status_code == status and res.json()["error"]["code"] == code
