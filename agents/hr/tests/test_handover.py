from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from hr_agent import handlers
from hr_agent.main import app


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    handlers.reset_state()


def _body(skill: str, input_: dict, key: str, token: str = "mock:dashboard-api") -> dict:
    return {
        "meta": {
            "context_id": "ctx_req_0777",
            "token": token,
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input_,
    }


def _start(client: TestClient) -> dict:
    return client.post(
        "/a2a", json=_body("start_handover", {"owner_id": "m-1"}, "ctx_req_0777:start:m-1")
    ).json()


def test_start_returns_questions_as_input_required() -> None:
    result = _start(TestClient(app))
    assert (result["status"], result["reason_code"]) == ("input-required", "human_input")
    out = result["output"]
    assert out["gap_count"] == 2
    assert out["completeness"] == 0
    assert {q["context_id"] for q in out["questions"]} == {"ctx_req_0777"}


def test_answer_updates_completeness_and_is_idempotent() -> None:
    client = TestClient(app)
    first_q = _start(client)["output"]["questions"][0]["question_id"]
    body = _body(
        "answer_handover_question",
        {"question_id": first_q, "answer": "m-2"},
        "ctx_req_0777:answer:" + first_q,
    )
    first = client.post("/a2a", json=body).json()
    again = client.post("/a2a", json=body).json()
    assert first["output"]["completeness"] == 50
    assert len(first["output"]["remaining_questions"]) == 1
    assert again["outcome"] == "duplicate"
    assert again["output"] == first["output"]


def test_answering_all_questions_completes() -> None:
    client = TestClient(app)
    for q in _start(client)["output"]["questions"]:
        result = client.post(
            "/a2a",
            json=_body(
                "answer_handover_question",
                {"question_id": q["question_id"], "answer": "x"},
                "k:" + q["question_id"],
            ),
        ).json()
    assert result["status"] == "completed"
    assert result["output"]["completeness"] == 100


def test_unknown_question_fails() -> None:
    result = (
        TestClient(app)
        .post(
            "/a2a",
            json=_body("answer_handover_question", {"question_id": "q_none", "answer": "x"}, "k"),
        )
        .json()
    )
    assert result["outcome"] == "question_not_found"


def test_answer_skill_acl() -> None:
    body = _body(
        "answer_handover_question", {"question_id": "q", "answer": "x"}, "k", token="mock:finance"
    )
    result = TestClient(app).post("/a2a", json=body).json()
    assert (result["status"], result["outcome"]) == ("rejected", "FORBIDDEN_SKILL")
