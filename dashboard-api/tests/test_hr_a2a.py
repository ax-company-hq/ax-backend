"""목 모드가 아닐 때 인사 탭 경로가 hr skill을 부르고 계약 모델 그대로 돌려주는지 본다.

응답 dict는 hr 에이전트 출력(계약 `schemas.hr`, `docs/notes/hr/dashboard-fixtures/`)의 모양이다.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, InputReason, RequestMeta, TaskStatus
from ax_dashboard_api import hr_client
from ax_dashboard_api.envelope import ApiError
from ax_dashboard_api.main import app

BASE = "/api/dashboard"
NOW = "2026-10-22T09:00:00Z"

QUESTION = {
    "question_id": "qst_hnd_748c7c4358f6_known_risks",
    "context_id": "ctx_hnd_0001",
    "assignee_id": "emp_01",
    "question": "payment 영역의 '알려진 위험'이(가) 비어 있어요.",
    "gap_field": "known_risks",
}
HANDOVER = {
    "handover_id": "hnd_748c7c4358f6",
    "context_id": "ctx_hnd_0001",
    "owner_id": "emp_01",
    "successor_id": "emp_02",
    "area": "payment",
    "reason": "transfer",
    "status": "input_required",
    "completeness": 80,
    "items": [{"item_id": "pr_412", "kind": "pr", "title": "결제 재시도 로직 PR"}],
    "open_questions": [QUESTION],
    "created_at": NOW,
    "updated_at": NOW,
}
READY = {**HANDOVER, "handover_id": "hnd_ready", "status": "ready", "open_questions": []}
CARD = {
    "card_id": "card_d2c8a53d7ee0",
    "kind": "decision",
    "tenant_id": "shop-demo",
    "context_id": "ctx_dec_0003",
    "title": "[결정] 야간 알림 기준을 낮추기로 했다",
    "body": "결정: 야간 알림 기준을 낮추기로 했다",
    "tags": ["decision", "candidate"],
    "status": "active",
    "source_refs": [],
    "author": "support",
    "confidence": "low",
    "created_at": NOW,
    "candidate": True,
    "extra": {"area": "search"},
}
ANSWERED = {
    "handover_id": "hnd_748c7c4358f6",
    "status": "ready",
    "completeness": 100,
    "remaining_questions": [],
}


class Fake:
    """hr 대역. skill별 결과를 `results`에 넣어 둔다 (기본은 `completed` + 출력)."""

    def __init__(self) -> None:
        self.sent: list[AgentRequest] = []
        self.outputs: dict[str, dict[str, Any]] = {}
        self.overrides: dict[str, dict[str, Any]] = {}  # skill -> AgentResult 필드

    async def __call__(self, target: str, req: AgentRequest) -> AgentResult:
        assert target == "hr"
        self.sent.append(req)
        fields = {"status": TaskStatus.COMPLETED, "output": self.outputs.get(req.skill, {})}
        return AgentResult(meta=req.meta, **{**fields, **self.overrides.get(req.skill, {})})

    def last(self, skill: str) -> AgentRequest:
        return [r for r in self.sent if r.skill == skill][-1]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "test-token")
    fake = Fake()
    monkeypatch.setattr(hr_client, "SEND", fake)
    return fake


@pytest.fixture
def client(fake: Fake) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------- 인수인계 조회
def test_handovers_are_listed_with_filters_and_the_contract_shape(
    client: TestClient, fake: Fake
) -> None:
    fake.outputs["list_handovers"] = {"handovers": [HANDOVER, READY], "collected_at": NOW}
    response = client.get(BASE + "/handovers", params={"status": "ready", "area": "payment"})
    body = response.json()
    assert response.status_code == 200 and body["meta"]["total"] == 2
    assert body["data"][0]["open_questions"][0]["context_id"] == "ctx_hnd_0001"
    sent = fake.last("list_handovers")
    assert sent.input == {"status": "ready", "area": "payment", "limit": 200}
    assert sent.meta.token == "test-token"  # 요청자는 토큰에서 정해진다
    assert sent.meta.context_id.startswith("ctx_ui_")


def test_one_handover_is_found_in_the_list_or_is_404(client: TestClient, fake: Fake) -> None:
    fake.outputs["list_handovers"] = {"handovers": [HANDOVER], "collected_at": NOW}
    found = client.get(BASE + "/handovers/hnd_748c7c4358f6").json()
    assert found["data"]["owner_id"] == "emp_01"
    missing = client.get(BASE + "/handovers/hnd_nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


def test_open_questions_are_listed_with_their_context_ids(client: TestClient, fake: Fake) -> None:
    fake.outputs["list_open_questions"] = {"questions": [QUESTION], "collected_at": NOW}
    body = client.get(BASE + "/handover-questions", params={"assignee_id": "emp_01"}).json()
    assert body["data"] == [QUESTION] and body["meta"]["total"] == 1
    assert fake.last("list_open_questions").input == {"assignee_id": "emp_01"}


# ---------------------------------------------------------------- 답변
def answer(
    client: TestClient, text: str = "  모니터링 알림이 아직 꺼져 있어요  ", key: str = "k-1"
):
    qid = QUESTION["question_id"]
    return client.post(
        f"{BASE}/handover-questions/{qid}/answer",
        json={"question_id": qid, "answer": text, "idempotency_key": key},
    )


def test_answer_goes_to_hr_with_the_questions_own_context_id(
    client: TestClient, fake: Fake
) -> None:
    fake.outputs["list_open_questions"] = {"questions": [QUESTION], "collected_at": NOW}
    fake.outputs["answer_handover_question"] = ANSWERED
    response = answer(client)
    assert response.status_code == 200
    assert response.json()["data"] == ANSWERED
    sent = fake.last("answer_handover_question")
    assert sent.meta.context_id == "ctx_hnd_0001"  # 새로 만들지 않고 질문의 사건을 그대로
    assert sent.meta.idempotency_key == "k-1"
    assert sent.input == {
        "question_id": QUESTION["question_id"],
        "answer": "모니터링 알림이 아직 꺼져 있어요",
    }
    assert sent.untrusted == {}


def test_a_resent_answer_for_a_question_no_longer_open_still_reaches_hr(
    client: TestClient, fake: Fake
) -> None:
    """답해서 대기 목록에서 빠진 질문은 사건을 몰라 새 사건으로 보낸다. hr이 한 번만 반영한다."""
    fake.outputs["list_open_questions"] = {"questions": [], "collected_at": NOW}
    fake.outputs["answer_handover_question"] = ANSWERED
    fake.overrides["answer_handover_question"] = {"outcome": "duplicate"}
    response = answer(client)
    assert response.status_code == 200 and response.json()["data"] == ANSWERED
    assert fake.last("answer_handover_question").meta.context_id.startswith("ctx_ui_")


def test_invalid_answers_never_reach_hr(client: TestClient, fake: Fake) -> None:
    assert answer(client, text="   ").status_code == 422
    assert answer(client, key=" ").status_code == 422
    assert fake.sent == []


def test_a_different_answer_to_an_answered_question_is_409(client: TestClient, fake: Fake) -> None:
    fake.outputs["list_open_questions"] = {"questions": [], "collected_at": NOW}
    fake.overrides["answer_handover_question"] = {
        "outcome": "CONFLICT",
        "output": {"message": "이미 다른 답변이 반영된 질문이다"},
    }
    response = answer(client)
    assert response.status_code == 409 and response.json()["error"]["code"] == "CONFLICT"
    assert "다른 답변" in response.json()["error"]["message"]


def test_an_unknown_question_is_404(client: TestClient, fake: Fake) -> None:
    fake.outputs["list_open_questions"] = {"questions": [], "collected_at": NOW}
    fake.overrides["answer_handover_question"] = {
        "outcome": "UNKNOWN_QUESTION",
        "output": {"message": "없는 질문이다"},
    }
    assert answer(client).status_code == 404


# ---------------------------------------------------------------- 후보 카드
def test_candidate_cards_are_listed_by_area(client: TestClient, fake: Fake) -> None:
    fake.outputs["list_candidate_cards"] = {"cards": [CARD], "collected_at": NOW}
    body = client.get(BASE + "/candidate-cards", params={"area": "search"}).json()
    assert body["data"][0]["card_id"] == CARD["card_id"] and body["meta"]["total"] == 1
    assert fake.last("list_candidate_cards").input == {"area": "search"}
    client.get(BASE + "/candidate-cards")
    assert fake.last("list_candidate_cards").input == {}


def test_deciding_a_card_calls_hr_with_a_target_key(client: TestClient, fake: Fake) -> None:
    fake.outputs["decide_candidate_card"] = {"card_id": CARD["card_id"], "status": "retired"}
    response = client.post(
        f"{BASE}/candidate-cards/{CARD['card_id']}/decision",
        json={"decision": "reject", "note": "  근거가 부족해요 "},
    )
    assert response.status_code == 200 and response.json()["data"]["status"] == "retired"
    sent = fake.last("decide_candidate_card")
    assert sent.input == {
        "card_id": CARD["card_id"],
        "decision": "reject",
        "note": "근거가 부족해요",
    }
    assert (
        sent.meta.idempotency_key
        == f"{sent.meta.context_id}:decide_candidate_card:card:{CARD['card_id']}:reject"
    )
    assert sent.meta.token == "test-token"


def test_the_opposite_decision_is_409_and_a_bad_decision_never_reaches_hr(
    client: TestClient, fake: Fake
) -> None:
    fake.overrides["decide_candidate_card"] = {
        "outcome": "NOT_CANDIDATE",
        "output": {"message": "후보 카드가 아니다: retired"},
    }
    url = f"{BASE}/candidate-cards/{CARD['card_id']}/decision"
    assert client.post(url, json={"decision": "approve"}).status_code == 409
    assert client.post(url, json={"decision": "maybe"}).status_code == 422
    assert len(fake.sent) == 1


# ---------------------------------------------------------------- 오류 매핑 (hr 계약 5장)
def result(status: TaskStatus, outcome: str | None = None, **fields: Any) -> AgentResult:
    meta = RequestMeta(context_id="ctx_ui_x", token="t", deadline="2030-01-01T00:00:00Z")
    return AgentResult(meta=meta, status=status, outcome=outcome, **fields)


@pytest.mark.parametrize(
    ("made", "status", "code"),
    [
        (
            result(TaskStatus.REJECTED, "INVALID_INPUT", output={"message": "x"}),
            422,
            "INVALID_INPUT",
        ),
        (result(TaskStatus.REJECTED, "FORBIDDEN_SKILL"), 403, "UPSTREAM_REJECTED"),
        (result(TaskStatus.REJECTED, "UNAUTHORIZED"), 403, "UPSTREAM_REJECTED"),
        (result(TaskStatus.COMPLETED, "UNKNOWN_CARD"), 404, "NOT_FOUND"),
        (result(TaskStatus.COMPLETED, "UNKNOWN_STEP"), 404, "NOT_FOUND"),
        (result(TaskStatus.COMPLETED, "CONFLICT"), 409, "CONFLICT"),
        (result(TaskStatus.COMPLETED, "NOT_CANDIDATE"), 409, "CONFLICT"),
        (result(TaskStatus.COMPLETED, "HANDOVER_CLOSED"), 409, "CONFLICT"),
        (result(TaskStatus.COMPLETED, "gate_rejected"), 409, "NOT_EXECUTED"),
        (result(TaskStatus.FAILED, "UPSTREAM_UNAVAILABLE"), 502, "UPSTREAM_ERROR"),
        (result(TaskStatus.CANCELED, "TIMEOUT"), 504, "UPSTREAM_TIMEOUT"),
    ],
)
def test_hr_results_map_to_api_errors(made: AgentResult, status: int, code: str) -> None:
    with pytest.raises(ApiError) as caught:
        hr_client.unwrap(made)
    assert (caught.value.status_code, caught.value.code) == (status, code)


@pytest.mark.parametrize("outcome", ["answered", "duplicate", "ready", None])
def test_successful_outcomes_return_the_output(outcome: str | None) -> None:
    assert hr_client.unwrap(result(TaskStatus.COMPLETED, outcome, output={"a": 1})) == {"a": 1}


def test_input_required_is_data_not_an_error() -> None:
    held = result(
        TaskStatus.INPUT_REQUIRED,
        "gate_held",
        reason_code=InputReason.HUMAN_INPUT,
        output={"review_id": "rev_1"},
    )
    with pytest.raises(
        ApiError
    ):  # 관문 보류는 대시보드가 보류 큐에서 처리하므로 실행 안 됨으로 알린다
        hr_client.unwrap(held)
    waiting = result(
        TaskStatus.INPUT_REQUIRED, reason_code=InputReason.HUMAN_INPUT, output={"questions": []}
    )
    assert hr_client.unwrap(waiting) == {"questions": []}
