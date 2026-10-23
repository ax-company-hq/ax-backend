"""목 모드가 아닐 때 문의 API가 support skill을 올바로 부르고 결과를 오류로 바꾸는지 본다."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from ax_dashboard_api import support_client
from ax_dashboard_api.main import app

BASE = "/api/dashboard"
ITEM = {
    "id": 7,
    "customer_name": "김하늘",
    "subject": "배송 문의",
    "status": "open",
    "updated_at": "2026-10-22T00:00:00Z",
    "preview": "언제 오나요",
    "awaiting_reply": True,
}


class Fake:
    """support 에이전트 대역. `reply`를 바꿔 결과를 정한다."""

    def __init__(self) -> None:
        self.sent: list[AgentRequest] = []
        self.status = TaskStatus.COMPLETED
        self.outcome: str | None = None
        self.output: dict[str, Any] = {}

    async def __call__(self, target: str, req: AgentRequest) -> AgentResult:
        assert target == "support"
        self.sent.append(req)
        return AgentResult(
            meta=req.meta, status=self.status, outcome=self.outcome, output=self.output
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "test-token")
    fake = Fake()
    monkeypatch.setattr(support_client, "SEND", fake)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_list_calls_list_inquiries(client: TestClient, fake: Fake) -> None:
    fake.output = {"items": [ITEM]}
    body = client.get(BASE + "/conversations").json()
    assert body["data"][0]["id"] == 7
    assert body["meta"]["total"] == 1
    sent = fake.sent[0]
    assert sent.skill == "list_inquiries"
    assert sent.meta.context_id.startswith(support_client.UI_CONTEXT_PREFIX)
    assert sent.meta.token == "test-token"
    assert sent.meta.idempotency_key is None


def test_create_passes_text_only_in_untrusted(client: TestClient, fake: Fake) -> None:
    fake.output = {**ITEM, "messages": []}
    response = client.post(
        BASE + "/conversations",
        json={"user_id": 1, "subject": "제목", "message": "본문", "order_id": 101},
    )
    assert response.status_code == 201
    sent = fake.sent[0]
    assert sent.skill == "create_inquiry"
    assert sent.untrusted == {"subject": "제목", "message": "본문"}
    assert sent.input == {"user_id": 1, "customer_name": "고객 1", "order_id": 101}
    assert sent.meta.idempotency_key == f"{sent.meta.context_id}:create_inquiry:user:1"


def test_agent_message_sends_draft_info(client: TestClient, fake: Fake) -> None:
    fake.output = {**ITEM, "messages": []}
    client.post(
        BASE + "/conversations/7/agent-messages",
        json={"text": "안내드립니다", "draft_style": "concise", "draft_text": "안내"},
    )
    sent = fake.sent[0]
    assert sent.skill == "send_reply"
    assert sent.input == {"inquiry_id": 7, "draft_style": "concise", "draft_text": "안내"}
    assert sent.untrusted == {"text": "안내드립니다"}


def test_guard_block_becomes_422(client: TestClient, fake: Fake) -> None:
    fake.outcome = "blocked_by_guard"
    fake.output = {
        "validation": {
            "passed": False,
            "findings": [{"code": "promise_refund", "level": "error", "message": "환불 약속"}],
        }
    }
    response = client.post(BASE + "/conversations/7/agent-messages", json={"text": "무조건 환불"})
    assert response.status_code == 422
    assert response.json()["error"] == {"code": "REPLY_BLOCKED", "message": "환불 약속"}


def test_not_found_becomes_404(client: TestClient, fake: Fake) -> None:
    fake.outcome = "not_found"
    fake.output = {"message": "문의 9를 찾을 수 없다"}
    assert client.get(BASE + "/conversations/9").status_code == 404


def test_not_executed_becomes_409(client: TestClient, fake: Fake) -> None:
    fake.outcome = "not_executed_held"
    response = client.post(BASE + "/conversations/7/status", json={"status": "resolved"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_EXECUTED"


@pytest.mark.parametrize(
    ("status", "outcome", "code", "http"),
    [
        (TaskStatus.CANCELED, "TIMEOUT", "UPSTREAM_TIMEOUT", 504),
        (TaskStatus.FAILED, "unreachable", "UPSTREAM_ERROR", 502),
        (TaskStatus.REJECTED, "FORBIDDEN_SKILL", "UPSTREAM_REJECTED", 403),
    ],
)
def test_upstream_failures_map_to_errors(
    client: TestClient, fake: Fake, status: TaskStatus, outcome: str, code: str, http: int
) -> None:
    fake.status, fake.outcome = status, outcome
    response = client.get(BASE + "/conversations")
    assert response.status_code == http
    assert response.json()["error"]["code"] == code


def test_status_change_uses_execute_target_key(client: TestClient, fake: Fake) -> None:
    fake.output = ITEM
    data = client.post(BASE + "/conversations/7/status", json={"status": "resolved"}).json()
    assert data["data"]["id"] == 7
    assert fake.sent[0].input == {"inquiry_id": 7, "status": "resolved"}
    assert fake.sent[0].meta.idempotency_key.endswith(":set_inquiry_status:inquiry:7:resolved")


def test_other_routes_still_require_mock(client: TestClient, fake: Fake) -> None:
    response = client.get(BASE + "/reviews")  # 아직 support가 없는 경로
    assert response.status_code == 503
    assert fake.sent == []


def test_ui_contexts_never_use_a_prefix_owned_by_another_department(
    client: TestClient, fake: Fake
) -> None:
    """`ctx_req_`는 기획개발이 발급하는 변경 요청 사건이다. 화면 호출이 이를 흉내 내면 안 된다."""
    from ax_contracts.schemas.ids import ContextPrefix

    fake.output = {"items": []}
    for _ in range(3):
        client.get(BASE + "/conversations")
    contexts = [req.meta.context_id for req in fake.sent]
    assert len(set(contexts)) == 3  # 호출마다 새 사건
    for context in contexts:
        assert context.startswith(support_client.UI_CONTEXT_PREFIX)
        assert not any(context.startswith(reserved.value) for reserved in ContextPrefix)
