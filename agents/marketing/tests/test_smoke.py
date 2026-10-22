from fastapi.testclient import TestClient

import marketing_agent
from marketing_agent.main import app


def test_import() -> None:
    assert marketing_agent.__name__ == "marketing_agent"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
