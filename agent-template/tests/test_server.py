import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ax_agent_template import server
from ax_agent_template.server import create_agent_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_agent_card_and_health(client: TestClient) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "echo_agent"
    assert card["skills"][0]["allowed_requesters"] == ["orchestrator"]
    assert client.get("/health").json() == {"data": {"status": "ok"}, "meta": {}}


def test_happy_path_and_requester_comes_from_token(client, request_body) -> None:
    body = request_body(requester="finance")  # 본문의 requester는 무시한다
    result = client.post("/a2a", json=body).json()
    assert result["status"] == "completed"
    assert result["meta"]["requester"] == "orchestrator"
    assert result["output"]["input"] == {"n": 1}


def test_same_input_gives_same_result(client, request_body) -> None:
    first = client.post("/a2a", json=request_body()).json()
    second = client.post("/a2a", json=request_body()).json()
    assert first["output"] == second["output"]
    assert first["status"] == second["status"]


def test_missing_context_id_is_bad_request(client, request_body) -> None:
    body = request_body()
    del body["meta"]["context_id"]
    response = client.post("/a2a", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_CONTEXT"


def test_invalid_json_is_bad_request(client) -> None:
    response = client.post("/a2a", content=b"not json")
    assert response.status_code == 400


def test_bad_token_is_rejected(client, request_body) -> None:
    result = client.post("/a2a", json=request_body(token="plain-token")).json()
    assert result["status"] == "rejected"
    assert result["outcome"] == "UNAUTHORIZED"


def test_token_is_rejected_outside_mock_mode(client, request_body, real_mode) -> None:
    result = client.post("/a2a", json=request_body()).json()
    assert result["outcome"] == "UNAUTHORIZED"


def test_loop_is_rejected(client, request_body) -> None:
    body = request_body(path=["orchestrator", "echo_agent"])
    result = client.post("/a2a", json=body).json()
    assert result["status"] == "rejected"
    assert result["outcome"] == "LOOP_DETECTED"


def test_hop_limit_goes_to_escalation_queue(client, request_body) -> None:
    path = ["orchestrator", "planning", "support", "marketing"]
    result = client.post("/a2a", json=request_body(path=path)).json()
    assert result["status"] == "rejected"
    assert result["outcome"] == "HOP_LIMIT"
    assert len(server.MOCK_ESCALATIONS) == 1
    escalation = server.MOCK_ESCALATIONS[0]
    assert escalation.context_id == "ctx_req_test"
    assert escalation.path == path


def test_three_hops_is_still_allowed(client, request_body) -> None:
    path = ["orchestrator", "planning", "support"]
    assert client.post("/a2a", json=request_body(path=path)).json()["status"] == "completed"


def test_acl_and_unknown_skill_are_rejected(client, request_body) -> None:
    denied = client.post("/a2a", json=request_body(token="mock:finance")).json()
    assert (denied["status"], denied["outcome"]) == ("rejected", "FORBIDDEN_SKILL")
    unknown = client.post("/a2a", json=request_body(skill="nope")).json()
    assert (unknown["status"], unknown["outcome"]) == ("rejected", "FORBIDDEN_SKILL")


def test_expired_deadline_is_canceled(client, request_body) -> None:
    result = client.post("/a2a", json=request_body(deadline_in=-5)).json()
    assert result["status"] == "canceled"
    assert result["outcome"] == "TIMEOUT"


def test_handler_error_is_failed_without_leaking_details(client, request_body) -> None:
    result = client.post("/a2a", json=request_body("boom")).json()
    assert result["status"] == "failed"
    assert result["outcome"] == "handler_error"
    assert "의도한 오류" not in str(result)


def test_invalid_handler_output_is_failed(client, request_body) -> None:
    result = client.post("/a2a", json=request_body("broken_output")).json()
    assert (result["status"], result["outcome"]) == ("failed", "INVALID_OUTPUT")


def test_handlers_must_match_card_skills(card, handlers) -> None:
    with pytest.raises(ValueError, match="핸들러 없음"):
        create_agent_app(card, {"echo": handlers["echo"]})
    with pytest.raises(ValueError, match="카드에 없음"):
        create_agent_app(card, {**handlers, "extra": handlers["echo"]})
