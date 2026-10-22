import copy

import pytest
from fastapi.testclient import TestClient

from ax_dashboard_api import mock_data
from ax_dashboard_api.main import app

BASE = "/api/dashboard"
_STATE = ("CONVERSATIONS", "CLAIMS", "CLAIM_AUDITS", "QUEUE", "REVIEWS")


@pytest.fixture(autouse=True)
def restore_mock_state():
    saved = {name: copy.deepcopy(getattr(mock_data, name)) for name in _STATE}
    yield
    for name, value in saved.items():
        target = getattr(mock_data, name)
        if isinstance(target, dict):
            target.clear()
            target.update(value)
        else:
            target[:] = value


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    return TestClient(app)


def test_cors_allows_dashboard_web_origin(client: TestClient) -> None:
    response = client.options(
        BASE + "/conversations",
        headers={
            "Origin": "http://localhost:3001",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3001"


def test_write_requires_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    response = TestClient(app).post(BASE + "/conversations/1/status", json={"status": "resolved"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "UPSTREAM_NOT_CONNECTED"


def test_create_conversation_then_visible_in_list(client: TestClient) -> None:
    response = client.post(
        BASE + "/conversations",
        json={"user_id": 1, "subject": "배송 문의", "message": "언제 오나요?"},
    )
    assert response.status_code == 201
    created = response.json()["data"]
    assert created["status"] == "open"
    assert created["awaiting_reply"] is True
    listed = client.get(BASE + "/conversations").json()["data"]
    assert any(c["id"] == created["id"] for c in listed)


def test_create_conversation_unknown_user_404(client: TestClient) -> None:
    response = client.post(
        BASE + "/conversations", json={"user_id": 999, "subject": "a", "message": "b"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_agent_reply_flow_with_guard(client: TestClient) -> None:
    blocked = client.post(
        BASE + "/conversations/1/check", json={"text": "무조건 환불해 드립니다"}
    ).json()["data"]
    assert blocked["passed"] is False
    rejected = client.post(
        BASE + "/conversations/1/agent-messages", json={"text": "무조건 환불해 드립니다"}
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "REPLY_BLOCKED"

    sent = client.post(
        BASE + "/conversations/1/agent-messages", json={"text": "확인 후 안내드리겠습니다."}
    )
    assert sent.status_code == 201
    conversation = sent.json()["data"]
    assert conversation["awaiting_reply"] is False
    assert conversation["messages"][-1]["role"] == "agent"


def test_customer_message_reopens_conversation(client: TestClient) -> None:
    client.post(BASE + "/conversations/1/status", json={"status": "resolved"})
    response = client.post(BASE + "/conversations/1/customer-messages", json={"text": "다시 문의"})
    data = response.json()["data"]
    assert data["status"] == "open"
    assert data["awaiting_reply"] is True


def test_status_rejects_unknown_value(client: TestClient) -> None:
    response = client.post(BASE + "/conversations/1/status", json={"status": "bogus"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_summary_is_stored_on_conversation(client: TestClient) -> None:
    summary = client.post(BASE + "/conversations/1/summary").json()["data"]
    assert summary["issue_ko"]
    assert client.get(BASE + "/conversations/1").json()["data"]["summary"] == summary


def test_claim_approve_then_receive_then_inspect(client: TestClient) -> None:
    approved = client.post(BASE + "/claims/1/approve", json={"note": "증빙 확인"}).json()["data"]
    assert approved["status"] == "awaiting_return"
    assert approved["audit"][-1]["action"] == "approve"

    received = client.post(BASE + "/claims/1/receive", json={"inspection": "pass"}).json()["data"]
    assert received["status"] == "return_received"

    inspected = client.post(
        BASE + "/claims/1/inspection", json={"inspection": "fail", "note": "파손"}
    ).json()["data"]
    assert inspected["inspection"] == "fail"
    assert inspected["inspection_note"] == "파손"


def test_claim_invalid_transition_409(client: TestClient) -> None:
    response = client.post(BASE + "/claims/2/approve", json={"note": ""})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_claim_reject(client: TestClient) -> None:
    rejected = client.post(BASE + "/claims/1/reject", json={"note": "기준 미달"}).json()["data"]
    assert rejected["status"] == "rejected"
    assert rejected["manager_approved"] is False


def test_claim_unknown_404(client: TestClient) -> None:
    assert client.post(BASE + "/claims/999/approve", json={"note": ""}).status_code == 404


def test_process_queue_pays_ready_entries(client: TestClient) -> None:
    run = client.post(BASE + "/queue-entries/process", json={}).json()["data"]
    assert run["paid"] == 1
    assert run["results"][0]["result"] == "paid"
    assert run["results"][0]["compensation"] == 2000
    assert run["queue"] == []
    assert client.get(BASE + "/claims/2").json()["data"]["status"] == "completed"


def test_process_queue_without_body(client: TestClient) -> None:
    assert client.post(BASE + "/queue-entries/process").status_code == 200


def test_review_status(client: TestClient) -> None:
    hidden = client.post(BASE + "/reviews/1/status", json={"status": "hidden"}).json()["data"]
    assert hidden["status"] == "hidden"
    assert client.post(BASE + "/reviews/999/status", json={"status": "hidden"}).status_code == 404
