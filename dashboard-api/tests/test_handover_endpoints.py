"""인사 탭(인수인계 질문·후보 카드) 목 엔드포인트. 형식은 계약 `schemas.hr`와 같아야 한다."""

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas.hr import (
    AnswerHandoverQuestionOutput,
    DecideCandidateCardOutput,
    HandoverQuestion,
    HandoverView,
)
from ax_contracts.schemas.record import Card
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


def decide(client: TestClient, card_id: str, decision: str = "approve", **extra):
    return client.post(
        f"{BASE}/candidate-cards/{card_id}/decision", json={"decision": decision, **extra}
    )


def test_list_matches_the_contract_models(client: TestClient) -> None:
    body = client.get(BASE + "/handovers").json()
    handovers = [HandoverView.model_validate(h) for h in body["data"]]
    assert body["meta"]["total"] == 2
    first = handovers[0]
    assert first.status == "input_required" and first.completeness == 50
    assert (first.owner_id, first.successor_id, first.area) == ("emp_01", "emp_02", "payment")
    assert len(first.open_questions) == 2
    assert handovers[1].status == "ready" and handovers[1].open_questions == []


def test_list_filters_by_status_and_area(client: TestClient) -> None:
    ready = client.get(BASE + "/handovers", params={"status": "ready"}).json()
    assert [h["handover_id"] for h in ready["data"]] == ["hov_0002"]
    payment = client.get(BASE + "/handovers", params={"area": "payment"}).json()
    assert [h["handover_id"] for h in payment["data"]] == ["hov_0001"]
    assert client.get(BASE + "/handovers", params={"status": "nope"}).status_code == 422


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
    other = client.get(BASE + "/handover-questions", params={"assignee_id": "emp_99"}).json()
    assert other["data"] == []


def test_answering_raises_completeness_until_the_handover_is_ready(client: TestClient) -> None:
    first = AnswerHandoverQuestionOutput.model_validate(answer(client, "hq_0001").json()["data"])
    assert first.status == "input_required" and first.completeness == 75
    assert [q.question_id for q in first.remaining_questions] == ["hq_0002"]

    last = AnswerHandoverQuestionOutput.model_validate(
        answer(client, "hq_0002", key="k-2").json()["data"]
    )
    assert last.status == "ready" and last.completeness == 100 and last.remaining_questions == []
    assert client.get(BASE + "/handover-questions").json()["data"] == []
    assert client.get(BASE + "/handovers/hov_0001").json()["data"]["open_questions"] == []


def test_same_answer_twice_is_applied_once(client: TestClient) -> None:
    first = answer(client, "hq_0001", key="a").json()
    second = answer(client, "hq_0001", key="b")  # 키가 달라도 같은 답이면 한 번만 반영
    assert second.status_code == 200 and second.json() == first
    assert len(client.get(BASE + "/handovers/hov_0001").json()["data"]["open_questions"]) == 1


def test_a_different_answer_to_an_answered_question_is_a_conflict(client: TestClient) -> None:
    answer(client, "hq_0001", text="첫 답")
    response = answer(client, "hq_0001", text="다른 답", key="k-2")
    assert response.status_code == 409 and response.json()["error"]["code"] == "CONFLICT"


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


# ---------------------------------------------------------------- 후보 카드
def test_candidate_cards_match_the_contract_model(client: TestClient) -> None:
    body = client.get(BASE + "/candidate-cards").json()
    cards = [Card.model_validate(c) for c in body["data"]]
    assert body["meta"]["total"] == 2
    assert all(c.candidate and c.confidence == "low" for c in cards)
    search = client.get(BASE + "/candidate-cards", params={"area": "search"}).json()
    assert [c["card_id"] for c in search["data"]] == ["card_d2c8a53d7ee0"]


def test_deciding_a_card_removes_it_from_the_queue(client: TestClient) -> None:
    approved = DecideCandidateCardOutput.model_validate(
        decide(client, "card_d2c8a53d7ee0", note="맞는 결정").json()["data"]
    )
    assert (approved.card_id, approved.status) == ("card_d2c8a53d7ee0", "active")
    rejected = decide(client, "card_77aa10c2be31", "reject").json()["data"]
    assert rejected["status"] == "retired"
    assert client.get(BASE + "/candidate-cards").json()["data"] == []


def test_same_decision_twice_succeeds_and_the_opposite_conflicts(client: TestClient) -> None:
    decide(client, "card_d2c8a53d7ee0")
    assert decide(client, "card_d2c8a53d7ee0").status_code == 200
    response = decide(client, "card_d2c8a53d7ee0", "reject")
    assert response.status_code == 409 and response.json()["error"]["code"] == "CONFLICT"


def test_unknown_card_is_404_and_a_bad_decision_is_422(client: TestClient) -> None:
    assert decide(client, "card_9999").status_code == 404
    assert decide(client, "card_d2c8a53d7ee0", "maybe").status_code == 422
