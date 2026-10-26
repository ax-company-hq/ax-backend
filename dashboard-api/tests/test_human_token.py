"""쓰기 API는 사람 토큰이 있어야 한다 (#295). 읽기는 그대로 열려 있다."""

import pytest
from fastapi.testclient import TestClient

from ax_dashboard_api.main import app

BASE = "/api/dashboard"
WRITES = [
    ("/claims/1/approve", {}),
    ("/queue-entries/process", {}),
    ("/conversations/1/status", {"status": "closed"}),
    ("/handover-questions/q1/answer", {}),
    ("/candidate-cards/card_1/decision", {"decision": "approve"}),
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    c = TestClient(app)
    c.headers.pop("authorization")
    return c


@pytest.mark.parametrize(("path", "body"), WRITES)
def test_a_write_without_a_token_is_401_in_the_error_format(client, path, body) -> None:
    res = client.post(BASE + path, json=body)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("token", ["mock:orchestrator", "mock:support"])
def test_an_agent_token_cannot_write(client, token) -> None:
    res = client.post(BASE + "/queue-entries/process", json={}, headers=auth(token))
    assert res.status_code == 403 and res.json()["error"]["code"] == "HUMAN_TOKEN_REQUIRED"


def test_an_unknown_token_is_refused_outside_the_demo_form(client, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_MOCK")
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    res = client.post(BASE + "/queue-entries/process", json={}, headers=auth("human:someone"))
    assert res.status_code == 403 and res.json()["error"]["code"] == "HUMAN_TOKEN_INVALID"


def test_with_no_configured_tokens_and_no_mock_every_write_is_refused(client, monkeypatch) -> None:
    monkeypatch.delenv("DASHBOARD_MOCK")
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.delenv("DASHBOARD_HUMAN_TOKENS", raising=False)
    monkeypatch.delenv("SAFETY_HUMAN_TOKENS", raising=False)
    res = client.post(BASE + "/queue-entries/process", json={}, headers=auth("anything"))
    assert res.status_code == 403


def test_the_demo_human_token_works_only_in_mock_mode(client) -> None:
    res = client.post(BASE + "/claims/999/approve", json={}, headers=auth("human:byunghwan"))
    assert res.status_code == 404


def test_a_configured_token_is_accepted_and_falls_back_to_the_safety_variable(
    client, monkeypatch
) -> None:
    monkeypatch.delenv("DASHBOARD_HUMAN_TOKENS")
    monkeypatch.setenv("SAFETY_HUMAN_TOKENS", "taeyoon:safe-token")
    res = client.post(BASE + "/claims/999/approve", json={}, headers=auth("safe-token"))
    assert res.status_code == 404


def test_reads_and_plan_checks_need_no_token(client) -> None:
    assert client.get(BASE + "/communications").status_code == 200
    res = client.post(BASE + "/plan-checks", json={"draft": {"title": "x"}})
    assert res.status_code == 422  # 토큰은 통과하고 본문 검증에서 멈춘다


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
