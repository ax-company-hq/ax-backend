"""인사 탭 나머지 경로(온보딩·변경·명부·감사 기록·계획 비교): 목 모드와 hr 연결 모두."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from ax_contracts.schemas.hr import (
    AuditEntry,
    CheckPlanOutput,
    GetRosterOutput,
)
from ax_dashboard_api import hr_client, mock_hr
from ax_dashboard_api.main import app

BASE = "/api/dashboard"
NOW = "2026-10-22T09:00:00Z"
STEP = {
    "step_id": "stp_1",
    "order": 1,
    "title": "명부를 훑어본다",
    "card_id": None,
    "done": True,
}
ONBOARDING = {
    "onboarding_id": "onb_1",
    "context_id": "ctx_onb_0001",
    "member_id": "emp_10",
    "area": "payment",
    "handover_id": None,
    "steps": [STEP],
    "progress": 100,
    "created_at": NOW,
}
CHANGE = {
    "change_id": "chg_1",
    "context_id": "ctx_asg_0001",
    "change_kind": "offboarding",
    "member_id": "emp_03",
    "area": "search",
    "successor_id": "emp_04",
    "status": "requested",
    "handover_id": None,
    "onboarding_id": None,
    "checklist": [{"item_id": "chk_1", "title": "후임을 지정한다", "done": False}],
    "created_at": NOW,
    "updated_at": NOW,
}
ROSTER = {
    "entries": [
        {
            "area": "payment",
            "role": "owner",
            "member_id": "emp_01",
            "shift_start": NOW,
            "shift_end": "2026-10-22T18:00:00Z",
        }
    ],
    "gap_areas": ["search"],
    "collected_at": NOW,
}
AUDIT = {
    "context_id": "ctx_hnd_0001",
    "entries": [{"seq": 1, "at": NOW, "kind": "start_handover", "payload": {"id": "hnd_1"}}],
    "collected_at": NOW,
}
PLAN = {
    "verdict": "warn",
    "findings": [
        {
            "code": "REJECTED_BEFORE",
            "level": "warn",
            "message": "이미 기각된 안과 비슷하다",
            "card_ids": ["card_1"],
        }
    ],
    "similar_cards": [{"card_id": "card_1", "summary": "캐시 도입안", "status": "active"}],
    "computed": {"days_since_rejected": 3, "basis": "기각 카드 card_1의 결정 시각"},
    "no_related_cards": False,
}
DRAFT = {"title": "결제 서버 캐시 도입", "summary": "응답 속도", "area": "payment", "kind": "spec"}


# ================================================================ 목 모드
@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    monkeypatch.setattr(mock_hr, "BOARD", mock_hr.HrBoard())
    return TestClient(app)


def test_mock_lists_match_the_contract_models(mock_client: TestClient) -> None:
    # 온보딩·발령은 hr에서 빠져 폐기 예정이다 (#374·#377): 목 모드도 빈 목록을 돌려준다
    onboardings = mock_client.get(BASE + "/onboardings").json()
    assert onboardings["data"] == [] and onboardings["meta"]["total"] == 0
    changes = mock_client.get(BASE + "/assignment-changes").json()
    assert changes["data"] == [] and changes["meta"]["total"] == 0
    roster = GetRosterOutput.model_validate(mock_client.get(BASE + "/roster").json()["data"])
    assert roster.gap_areas == ["search"] and {e.area for e in roster.entries} >= {"payment"}


def test_mock_filters(mock_client: TestClient) -> None:
    get = mock_client.get
    assert get(BASE + "/onboardings", params={"member_id": "emp_99"}).json()["data"] == []
    only = get(BASE + "/assignment-changes", params={"status": "confirmed"}).json()["data"]
    assert only == []  # 폐기 예정이라 필터와 상관없이 비어 있다
    assert get(BASE + "/assignment-changes", params={"status": "nope"}).status_code == 422
    payment = GetRosterOutput.model_validate(
        get(BASE + "/roster", params={"area": "payment"}).json()["data"]
    )
    assert {e.area for e in payment.entries} == {"payment"} and payment.gap_areas == []


def test_mock_step_completion_recounts_progress_and_can_be_undone(mock_client: TestClient) -> None:
    # 단계 완료 표시는 폐기됐다: 어떤 본문이든 410이다
    url = BASE + "/onboardings/onb_0001/steps/stp_onb_0001_2"
    gone = mock_client.post(url, json={})
    assert gone.status_code == 410 and gone.json()["error"]["code"] == "ENDPOINT_RETIRED"
    assert mock_client.post(url, json={"done": True}).status_code == 410
    assert mock_client.post(url, json={"done": False}).status_code == 410


def test_mock_unknown_onboarding_or_step_is_404(mock_client: TestClient) -> None:
    # 없는 온보딩·단계도 폐기 경로라 404가 아니라 410이다
    assert mock_client.post(BASE + "/onboardings/onb_9/steps/stp_1", json={}).status_code == 410
    unknown = mock_client.post(BASE + "/onboardings/onb_0001/steps/stp_9", json={})
    assert unknown.status_code == 410 and unknown.json()["error"]["code"] == "ENDPOINT_RETIRED"
    assert "인사 에이전트에서 빠졌다" in unknown.json()["error"]["message"]


def test_mock_activity_is_listed_by_context(mock_client: TestClient) -> None:
    body = mock_client.get(BASE + "/hr-activity/ctx_hnd_0001").json()
    assert [AuditEntry.model_validate(e).seq for e in body["data"]] == [1, 2]
    assert body["meta"]["total"] == 2
    assert mock_client.get(BASE + "/hr-activity/ctx_none").json()["data"] == []


def test_mock_plan_check_warns_on_a_rejected_idea(mock_client: TestClient) -> None:
    warn = CheckPlanOutput.model_validate(
        mock_client.post(BASE + "/plan-checks", json={"draft": DRAFT}).json()["data"]
    )
    assert warn.verdict == "warn" and warn.findings[0].code == "REJECTED_BEFORE"
    ok = mock_client.post(
        BASE + "/plan-checks", json={"draft": {**DRAFT, "title": "검색 개선", "summary": "랭킹"}}
    ).json()["data"]
    assert ok["verdict"] == "ok" and ok["no_related_cards"] is True
    bad = mock_client.post(BASE + "/plan-checks", json={"draft": {**DRAFT, "kind": "memo"}})
    assert bad.status_code == 422


# ================================================================ hr 연결
class Fake:
    """hr 대역. skill별 출력·결과 필드를 넣어 둔다."""

    def __init__(self) -> None:
        self.sent: list[AgentRequest] = []
        self.outputs: dict[str, dict[str, Any]] = {}
        self.overrides: dict[str, dict[str, Any]] = {}

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


def test_onboardings_are_listed_with_filters(client: TestClient, fake: Fake) -> None:
    body = client.get(BASE + "/onboardings", params={"member_id": "emp_10"}).json()
    assert body["data"] == [] and body["meta"]["total"] == 0  # 폐기 예정: 빈 목록
    assert fake.sent == []  # hr은 이 skill이 없으니 부르지 않는다


def test_step_completion_goes_to_hr_with_a_target_key(client: TestClient, fake: Fake) -> None:
    response = client.post(BASE + "/onboardings/onb_1/steps/stp_1", json={"done": False})
    assert response.status_code == 410 and response.json()["error"]["code"] == "ENDPOINT_RETIRED"
    assert fake.sent == []  # hr을 부르지 않는다
    assert "폐기 예정" in response.json()["error"]["message"]  # 이유를 쉬운 말로 알린다


def test_an_unknown_step_is_404_from_hr(client: TestClient, fake: Fake) -> None:
    response = client.post(BASE + "/onboardings/onb_1/steps/stp_9", json={})
    assert response.status_code == 410 and response.json()["error"]["code"] == "ENDPOINT_RETIRED"
    assert fake.sent == []


def test_changes_are_listed_with_filters(client: TestClient, fake: Fake) -> None:
    body = client.get(
        BASE + "/assignment-changes", params={"status": "requested", "area": "search"}
    ).json()
    assert body["data"] == [] and body["meta"]["total"] == 0  # 폐기 예정: 빈 목록
    assert fake.sent == []


def test_roster_is_one_object_with_gap_areas(client: TestClient, fake: Fake) -> None:
    fake.outputs["get_roster"] = ROSTER
    data = client.get(BASE + "/roster", params={"area": "payment"}).json()["data"]
    assert data["gap_areas"] == ["search"] and data["entries"][0]["member_id"] == "emp_01"
    assert fake.last("get_roster").input == {"area": "payment"}


def test_activity_is_the_audit_timeline_of_the_context(client: TestClient, fake: Fake) -> None:
    fake.outputs["get_audit_timeline"] = AUDIT
    body = client.get(BASE + "/hr-activity/ctx_hnd_0001").json()
    assert body["data"][0]["kind"] == "start_handover" and body["meta"]["total"] == 1
    assert fake.last("get_audit_timeline").input == {"context_id": "ctx_hnd_0001"}


def test_plan_check_sends_the_draft_as_input(client: TestClient, fake: Fake) -> None:
    fake.outputs["check_plan"] = PLAN
    response = client.post(BASE + "/plan-checks", json={"draft": DRAFT})
    assert response.status_code == 200 and response.json()["data"]["verdict"] == "warn"
    sent = fake.last("check_plan")
    assert sent.input == {"draft": {**DRAFT, "ref": None}}
    assert sent.untrusted == {}


def test_a_bad_plan_check_body_never_reaches_hr(client: TestClient, fake: Fake) -> None:
    assert client.post(BASE + "/plan-checks", json={"draft": {"title": "x"}}).status_code == 422
    assert client.get(BASE + "/assignment-changes", params={"status": "nope"}).status_code == 422
    assert fake.sent == []


def test_hr_failures_are_502_and_504(client: TestClient, fake: Fake) -> None:
    fake.overrides["get_roster"] = {"status": TaskStatus.FAILED, "outcome": "UPSTREAM_UNAVAILABLE"}
    assert client.get(BASE + "/roster").status_code == 502
    fake.overrides["get_roster"] = {"status": TaskStatus.CANCELED, "outcome": "TIMEOUT"}
    assert client.get(BASE + "/roster").status_code == 504
