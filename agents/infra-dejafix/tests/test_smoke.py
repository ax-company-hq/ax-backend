from fastapi.testclient import TestClient

import infra_agent
from infra_agent.main import app


def test_import() -> None:
    assert infra_agent.__name__ == "infra_agent"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
