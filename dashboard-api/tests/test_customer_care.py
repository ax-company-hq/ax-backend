"""고객관리 탭 읽기 API: 목 모드 응답과, 목 모드가 아닐 때 support skill 호출을 본다."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from ax_dashboard_api import support_client
from ax_dashboard_api.main import app

BASE = "/api/dashboard"


class Fake:
    def __init__(self) -> None:
        self.sent: list[AgentRequest] = []
        self.outputs: dict[str, dict[str, Any]] = {}

    async def __call__(self, target: str, req: AgentRequest) -> AgentResult:
        assert target == "support"
        self.sent.append(req)
        return AgentResult(
            meta=req.meta, status=TaskStatus.COMPLETED, output=self.outputs.get(req.skill, {})
        )


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    return TestClient(app)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "test-token")
    fake = Fake()
    monkeypatch.setattr(support_client, "SEND", fake)
    return fake


# ---------------------------------------------------------------- 목 모드
def test_at_risk_customers_are_listed_by_score_and_filtered_by_level(
    mock_client: TestClient,
) -> None:
    body = mock_client.get(BASE + "/customers/at-risk").json()
    assert [c["buyer_id"] for c in body["data"]] == ["buy_0001", "buy_0002"]
    assert body["meta"]["total"] == 2
    high = mock_client.get(BASE + "/customers/at-risk", params={"min_level": "high"}).json()
    assert [c["buyer_id"] for c in high["data"]] == ["buy_0001"]
    assert (
        mock_client.get(BASE + "/customers/at-risk", params={"min_level": "low"}).status_code == 422
    )
    assert mock_client.get(BASE + "/customers/at-risk", params={"limit": 0}).status_code == 422


def test_customer_signals_for_a_known_and_an_unknown_buyer(mock_client: TestClient) -> None:
    known = mock_client.get(BASE + "/customers/buy_0001/signals").json()["data"]
    assert known["level"] == "high" and known["recommended_action"] == "personal_followup"
    assert sum(r["points"] for r in known["reasons"]) == known["score"]
    unknown = mock_client.get(BASE + "/customers/buy_9999/signals").json()["data"]
    assert unknown["level"] == "low" and unknown["reasons"] == []


def test_compensation_suggestion_has_a_request_only_when_eligible(
    mock_client: TestClient,
) -> None:
    late = mock_client.get(BASE + "/conversations/1/compensation-suggestion").json()["data"]
    assert late["eligible"] is True and late["total_value"] == late["unit_value"] * late["count"]
    assert late["request"]["reason_code"] == "late_delivery"
    on_time = mock_client.get(BASE + "/conversations/2/compensation-suggestion").json()["data"]
    assert on_time["eligible"] is False and on_time["request"] is None
    assert mock_client.get(BASE + "/conversations/99/compensation-suggestion").status_code == 404


def test_payment_diagnosis_for_failed_paid_and_missing(mock_client: TestClient) -> None:
    failed = mock_client.get(BASE + "/conversations/1/payment-diagnosis").json()["data"]
    assert failed["situation"] == "payment_failed" and failed["next_action"] == "wait_and_retry"
    paid = mock_client.get(BASE + "/conversations/2/payment-diagnosis").json()["data"]
    assert paid["situation"] == "paid" and paid["next_action"] == "none"
    missing = mock_client.get(BASE + "/conversations/99/payment-diagnosis")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


def test_surge_and_change_requests_in_mock_mode(mock_client: TestClient) -> None:
    surge = mock_client.get(BASE + "/inquiry-surge").json()["data"]
    assert surge["surges"][0]["topic_code"] == "payment"
    assert surge["surges"][0]["count"] >= len(surge["surges"][0]["sample_inquiry_ids"])
    rows = mock_client.get(BASE + "/change-requests").json()
    assert rows["meta"]["total"] == 1 and rows["data"][0]["status"] == "requested"
    done = mock_client.get(BASE + "/change-requests", params={"status": "completed"}).json()
    assert done["data"] == []
    assert mock_client.get(BASE + "/change-requests", params={"status": "x"}).status_code == 422


# ---------------------------------------------------------------- support A2A
def test_at_risk_calls_support_with_the_filters_and_keeps_its_total(fake: Fake) -> None:
    fake.outputs["list_at_risk_customers"] = {"items": [{"buyer_id": "buy_0007"}], "total": 12}
    body = TestClient(app).get(BASE + "/customers/at-risk", params={"limit": 5}).json()
    assert body["data"] == [{"buyer_id": "buy_0007"}] and body["meta"]["total"] == 12
    sent = fake.sent[0]
    assert sent.skill == "list_at_risk_customers"
    assert sent.input == {"min_level": "medium", "limit": 5}
    assert sent.meta.idempotency_key is None  # 읽기라 멱등 키가 없다


@pytest.mark.parametrize(
    ("path", "skill", "payload"),
    [
        ("/customers/buy_0001/signals", "customer_signals", {"buyer_id": "buy_0001"}),
        ("/conversations/4/compensation-suggestion", "suggest_compensation", {"inquiry_id": 4}),
        ("/conversations/4/payment-diagnosis", "diagnose_payment", {"inquiry_id": 4}),
    ],
)
def test_single_lookups_pass_the_id_to_the_support_skill(
    fake: Fake, path: str, skill: str, payload: dict
) -> None:
    fake.outputs[skill] = {"message": "ok"}
    body = TestClient(app).get(BASE + path).json()
    assert body["data"] == {"message": "ok"}
    assert fake.sent[0].skill == skill and fake.sent[0].input == payload


def test_surge_and_change_requests_call_support(fake: Fake) -> None:
    fake.outputs["detect_inquiry_surge"] = {"surges": [], "total_in_window": 0}
    fake.outputs["list_change_requests"] = {"items": [{"inquiry_id": 3}]}
    client = TestClient(app)
    assert client.get(BASE + "/inquiry-surge").json()["data"]["total_in_window"] == 0
    rows = client.get(BASE + "/change-requests", params={"inquiry_id": 3}).json()
    assert rows["data"] == [{"inquiry_id": 3}] and rows["meta"]["total"] == 1
    assert [s.skill for s in fake.sent] == ["detect_inquiry_surge", "list_change_requests"]
    assert fake.sent[1].input == {"inquiry_id": 3}


def test_support_errors_become_api_errors(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_found(target: str, req: AgentRequest) -> AgentResult:
        return AgentResult(
            meta=req.meta,
            status=TaskStatus.COMPLETED,
            outcome="not_found",
            output={"message": "문의 9를 찾을 수 없다"},
        )

    monkeypatch.setattr(support_client, "SEND", not_found)
    res = TestClient(app).get(BASE + "/conversations/9/payment-diagnosis")
    assert res.status_code == 404 and res.json()["error"]["code"] == "NOT_FOUND"


def test_the_new_routes_are_reads_so_they_need_no_token(mock_client: TestClient) -> None:
    mock_client.headers.pop("authorization")
    assert mock_client.get(BASE + "/customers/at-risk").status_code == 200
