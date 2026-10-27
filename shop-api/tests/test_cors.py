"""브라우저(shop-web)가 shop-api를 직접 부를 수 있도록 CORS를 연다."""

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


def _preflight(client: TestClient, origin: str, path: str = "/api/shop/claims"):  # noqa: ANN202
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_shop_web_origin_may_call_the_api_from_the_browser() -> None:
    client = TestClient(create_app(Db(":memory:")))
    for origin in ("http://localhost:3000", "http://127.0.0.1:3000"):
        response = _preflight(client, origin)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert "POST" in response.headers["access-control-allow-methods"]
    simple = client.get("/api/shop/products", headers={"Origin": "http://localhost:3000"})
    assert simple.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_dashboard_web_origin_may_call_the_relay_from_the_browser() -> None:
    client = TestClient(create_app(Db(":memory:")))
    for origin in ("http://localhost:3001", "http://127.0.0.1:3001"):
        response = _preflight(client, origin)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
    # 허용 목록에 없는 포트는 여전히 거부된다
    assert _preflight(client, "http://localhost:3002").status_code == 400


def test_other_origins_get_no_cors_headers() -> None:
    client = TestClient(create_app(Db(":memory:")))
    response = _preflight(client, "https://evil.example")
    assert response.status_code == 400  # 허용하지 않은 출처의 사전 요청은 거부된다
    assert "access-control-allow-origin" not in response.headers
    plain = client.get("/api/shop/products", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in plain.headers


def test_origins_can_be_set_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOP_CORS_ORIGINS", "https://shop.example, https://staging.example")
    client = TestClient(create_app(Db(":memory:")))
    assert _preflight(client, "https://staging.example").status_code == 200
    assert _preflight(client, "http://localhost:3000").status_code == 400
