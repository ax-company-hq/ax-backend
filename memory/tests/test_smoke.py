from fastapi.testclient import TestClient

import ax_memory
from ax_memory.main import app


def test_import() -> None:
    assert ax_memory.__name__ == "ax_memory"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
