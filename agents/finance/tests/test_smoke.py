from fastapi.testclient import TestClient

import finance_agent
from finance_agent.main import app


def test_import() -> None:
    assert finance_agent.__name__ == "finance_agent"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
