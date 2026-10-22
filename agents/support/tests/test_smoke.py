from fastapi.testclient import TestClient

import support_agent
from support_agent.main import app


def test_import() -> None:
    assert support_agent.__name__ == "support_agent"


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": {}}
