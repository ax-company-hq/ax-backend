"""인사 탭(인수인계 질문) 목 엔드포인트. 형식은 계약 `ax_contracts.schemas.hr`와 같아야 한다."""

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas.hr import (
    AnswerHandoverQuestionOutput,
    HandoverQuestion,
    StartHandoverOutput,
)
from ax_dashboard_api import mock_handover
from ax_dashboard_api.main import app

BASE = "/api/dashboard"


@pytest.fixture(autouse=True)
def fresh_board(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mock_handover, "BOARD", mock_handover.HandoverBoard())


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    return TestClient(app)


def answer(client: TestClient, question_id: str, text: str = "확인 끝났습니다", key: str = "k-1"):
    return client.post(
        f"{BASE}/handover-questions/{question_id}/answer",
        json={"question_id": question_id, "answer": text, "idempotency_key": key},
    )


def test_list_matches_the_contract_models(client: TestClient) -> None:
    body = client.get(BASE + "/handovers").json()
    handovers = [StartHandoverOutput.model_validate(h) for h in body["data"]]
    assert body["meta"]["total"] == 2
    first = handovers[0]
    assert first.status == "input_required" and first.gap_count == 2 and first.completeness == 50
    assert len(first.questions) == first.gap_count  # 빈 자리 수는 질문 수와 같다
    assert handovers[1].status == "ready" and handovers[1].questions == []


def test_get_one_and_unknown_is_404(client: TestClient) -> None:
    assert client.get(BASE + "/handovers/hov_0001").json()["data"]["handover_id"] == "hov_0001"
    response = client.get(BASE + "/handovers/hov_9999")
    assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"


def test_pending_questions_carry_the_context_id_the_answer_must_use(client: TestClient) -> None:
    body = client.get(BASE + "/handover-questions").json()
    questions = [HandoverQuestion.model_validate(q) for q in body["data"]]
    assert [q.question_id for q in questions] == ["hq_0001", "hq_0002"]
    assert {q.context_id for q in questions} == {"ctx_hnd_0001"}
    assert all(q.assignee_id and q.question and q.gap_field for q in questions)


def test_answering_raises_completeness_until_the_handover_is_ready(client: TestClient) -> None:
    first = AnswerHandoverQuestionOutput.model_validate(answer(client, "hq_0001").json()["data"])
    assert first.status == "input_required" and first.completeness == 75
    assert [q.question_id for q in first.remaining_questions] == ["hq_0002"]

    last = AnswerHandoverQuestionOutput.model_validate(
        answer(client, "hq_0002", key="k-2").json()["data"]
    )
    assert last.status == "ready" and last.completeness == 100 and last.remaining_questions == []
    assert client.get(BASE + "/handover-questions").json()["data"] == []
    assert client.get(BASE + "/handovers/hov_0001").json()["data"]["gap_count"] == 0


def test_same_answer_twice_is_applied_once(client: TestClient) -> None:
    first = answer(client, "hq_0001", key="same").json()
    second = answer(client, "hq_0001", key="same")
    assert second.status_code == 200 and second.json() == first
    assert client.get(BASE + "/handovers/hov_0001").json()["data"]["gap_count"] == 1


def test_reusing_a_key_for_another_question_conflicts(client: TestClient) -> None:
    answer(client, "hq_0001", key="same")
    response = answer(client, "hq_0002", key="same")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_answering_an_answered_question_with_a_new_key_is_409(client: TestClient) -> None:
    answer(client, "hq_0001", key="k-1")
    response = answer(client, "hq_0001", key="k-2")
    assert response.status_code == 409 and response.json()["error"]["code"] == "INVALID_STATE"


def test_unknown_question_is_404(client: TestClient) -> None:
    assert answer(client, "hq_9999").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"question_id": "hq_0001", "answer": "   ", "idempotency_key": "k"},
        {"question_id": "hq_0001", "answer": "답", "idempotency_key": "  "},
        {"question_id": "hq_0002", "answer": "답", "idempotency_key": "k"},  # 경로와 다름
        {"question_id": "hq_0001", "answer": "답"},  # 키 없음
        {"answer": "답", "idempotency_key": "k"},  # 질문 ID 없음
    ],
)
def test_invalid_answer_requests_are_422_and_change_nothing(client: TestClient, body) -> None:
    response = client.post(f"{BASE}/handover-questions/hq_0001/answer", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert len(client.get(BASE + "/handover-questions").json()["data"]) == 2


def test_not_connected_outside_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    client = TestClient(app)
    for path in ("/handovers", "/handover-questions"):
        response = client.get(BASE + path)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "UPSTREAM_NOT_CONNECTED"
    post = answer(client, "hq_0001")
    assert post.status_code == 503
