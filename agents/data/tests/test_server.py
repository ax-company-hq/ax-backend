"""통합 에이전트 없이 A2A로 data 에이전트를 부르는 테스트 (LLM·지표·shop 모두 목)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_agent_template.llm import MockLLMClient
from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from data_agent import handlers
from data_agent.handlers import HANDLERS

CTX = "ctx_inc_0001"
_ALL = ["orchestrator", "infra", "support", "finance", "planning", "dashboard-api"]


def _skill(skill_id: str, allowed: list[str]) -> dict:
    # 채워진 카드(PR #70)와 같은 skill id·allowed_requesters. 입출력 스키마는 경로 문자열이다.
    return {
        "id": skill_id,
        "description": f"{skill_id} 테스트 skill. 상태를 바꾸지 않는다.",
        "input_schema": "contracts/src/ax_contracts/schemas/data.py#X",
        "output_schema": "contracts/src/ax_contracts/schemas/data.py#Y",
        "grade": "read",
        "allowed_requesters": allowed,
        "verifier": None,
        "priority": "required",
    }


CARD = AgentCard.model_validate(
    {
        "name": "data",
        "owner": "재현",
        "description": "데이터분석 테스트 카드",
        "url": "http://data:8106/a2a",
        "skills": [
            _skill("anomaly_detection", ["orchestrator", "infra"]),
            _skill("impact_analysis", ["orchestrator", "infra", "support", "finance", "planning"]),
            _skill("fraud_score", ["orchestrator", "finance"]),
            _skill("kpi_summary", ["orchestrator", "dashboard-api"]),
        ],
    }
)

INCIDENT = {"window_from": "2026-10-21T05:00:00Z", "window_to": "2026-10-21T05:30:00Z"}


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("DATA_MOCK", "1")
    monkeypatch.setattr(handlers, "get_llm", lambda: MockLLMClient())  # 응답 없음 -> 템플릿


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_agent_app(CARD, HANDLERS))


def _body(skill: str, input: dict, token: str = "mock:orchestrator", **meta) -> dict:
    return {
        "meta": {
            "context_id": CTX,
            "token": token,
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            **meta,
        },
        "skill": skill,
        "input": input,
    }


def test_impact_analysis_completed(client: TestClient) -> None:
    result = client.post("/a2a", json=_body("impact_analysis", INCIDENT)).json()
    assert result["status"] == "completed"
    assert result["meta"]["requester"] == "orchestrator"
    assert result["output"]["failed_order_count"] == 6
    assert result["output"]["damage_amount"] == 95000
    assert result["evidence"]["queries"]
    assert result["fallback"] is True  # mock LLM 응답 없음


def test_anomaly_detection_completed(client: TestClient) -> None:
    body = _body("anomaly_detection", INCIDENT | {"metrics": ["payment_fail_rate"]}, "mock:infra")
    result = client.post("/a2a", json=body).json()
    assert result["status"] == "completed"
    assert result["output"]["any_anomaly"] is True
    assert result["output"]["verdicts"][0]["severity"] == "high"


def test_invalid_input_fails_without_crashing(client: TestClient) -> None:
    result = client.post("/a2a", json=_body("impact_analysis", {"window_from": "x"})).json()
    assert result["status"] == "failed"
    assert result["outcome"] == "invalid_input"


def test_reversed_window_is_invalid(client: TestClient) -> None:
    reversed_window = {"window_from": INCIDENT["window_to"], "window_to": INCIDENT["window_from"]}
    result = client.post("/a2a", json=_body("anomaly_detection", reversed_window)).json()
    assert result["outcome"] == "invalid_input"


def test_not_mock_env_is_unavailable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_MOCK")
    result = client.post("/a2a", json=_body("impact_analysis", INCIDENT)).json()
    assert result["status"] == "failed"
    assert result["outcome"] == "unavailable"


def test_unimplemented_skill_is_rejected_cleanly(client: TestClient) -> None:
    result = client.post("/a2a", json=_body("fraud_score", {"seller_id": "sel_1"})).json()
    assert result["status"] == "failed"
    assert result["outcome"] == "not_implemented"


def test_loop_is_rejected(client: TestClient) -> None:
    result = client.post("/a2a", json=_body("impact_analysis", INCIDENT, path=["data"])).json()
    assert result["status"] == "rejected"
    assert result["outcome"] == "LOOP_DETECTED"


def test_requester_not_in_acl_is_rejected(client: TestClient) -> None:
    # anomaly_detection은 orchestrator·infra만 허용한다
    body = _body("anomaly_detection", INCIDENT, token="mock:support")
    result = client.post("/a2a", json=body).json()
    assert result["status"] == "rejected"
    assert result["outcome"] == "FORBIDDEN_SKILL"


def test_bad_token_is_unauthorized(client: TestClient) -> None:
    result = client.post("/a2a", json=_body("impact_analysis", INCIDENT, token="nope")).json()
    assert result["outcome"] == "UNAUTHORIZED"


def test_expired_deadline_is_canceled(client: TestClient) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    result = client.post("/a2a", json=_body("impact_analysis", INCIDENT, deadline=past)).json()
    assert result["status"] == "canceled"
    assert result["outcome"] == "TIMEOUT"
