import socket

import pytest
from fastapi.testclient import TestClient

from ax_orchestrator.__main__ import free_port
from ax_orchestrator.main import app


def test_free_port_skips_a_port_that_is_in_use() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert free_port(taken) != taken


def test_free_port_gives_up_when_everything_is_taken() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(RuntimeError, match="포트가 모두 사용 중"):
            free_port(busy.getsockname()[1], tries=1)


def test_root_redirects_to_the_demo_page() -> None:
    response = TestClient(app).get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/demo"
