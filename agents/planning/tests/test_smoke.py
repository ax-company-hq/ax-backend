from fastapi.testclient import TestClient

import planning_agent
from planning_agent.main import app


def test_import() -> None:
    assert planning_agent.__name__ == "planning_agent"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
