from fastapi.testclient import TestClient

import hr_agent
from hr_agent.main import app


def test_import() -> None:
    assert hr_agent.__name__ == "hr_agent"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
