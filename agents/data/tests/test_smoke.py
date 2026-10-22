from fastapi.testclient import TestClient

import data_agent
from data_agent.main import app


def test_import() -> None:
    assert data_agent.__name__ == "data_agent"

