from fastapi.testclient import TestClient

import ax_safety
from ax_safety.main import app


def test_import() -> None:
    assert ax_safety.__name__ == "ax_safety"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
