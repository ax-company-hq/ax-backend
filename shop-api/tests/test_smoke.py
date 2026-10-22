from fastapi.testclient import TestClient

import ax_shop_api
from ax_shop_api.main import app


def test_import() -> None:
    assert ax_shop_api.__name__ == "ax_shop_api"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
