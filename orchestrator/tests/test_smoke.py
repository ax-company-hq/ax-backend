from fastapi.testclient import TestClient

import ax_orchestrator
from ax_orchestrator.main import app


def test_import() -> None:
    assert ax_orchestrator.__name__ == "ax_orchestrator"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
