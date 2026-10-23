"""시나리오 A 단독 테스트: 통합 에이전트 없이 A2A 요청만으로 영향 분석이 되는지 증명한다.

기획서 5장 시나리오 A 2단계("데이터분석 — 영향 분석: 결제 실패 건수, 영향 고객 수 산출").
실제 `agent-card.json`과 실제 앱(`data_agent.main`)을 쓰고, 요청은 agent-template의
테스트 클라이언트(`ax-test-call`)가 만드는 것과 같은 형식으로 보낸다. LLM만 목으로 바꾼다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import testclient
from ax_agent_template.llm import MockLLMClient
from data_agent import handlers, seed
from data_agent.main import build_app

INCIDENT_CTX = "ctx_inc_scn_a_0001"
INCIDENT = {
    "window_from": seed.INCIDENT_FROM.isoformat(),
    "window_to": seed.INCIDENT_TO.isoformat(),
    "scope": "payment",
}


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_MOCK", "1")  # 목 shop·지표, 토큰은 mock:<이름>
    monkeypatch.setattr(handlers, "get_llm", lambda: MockLLMClient())  # 응답 없음 → 템플릿 문장


@pytest.fixture
def agent() -> TestClient:
    return TestClient(build_app())


def _call(agent: TestClient, tmp_path: Path, skill: str, data: dict, *argv: str) -> dict:
    """`ax-test-call --agent ... --skill ... --input ...`과 같은 요청을 보낸다."""
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(data), encoding="utf-8")
    args = testclient.build_parser().parse_args(
        ["--agent", "http://data:8106", "--skill", skill, "--input", str(input_file), *argv]
    )
    request = testclient.build_request(args)
    response = agent.post("/a2a", json=request.model_dump(mode="json"))
    assert response.status_code == 200
    return response.json()


def test_agent_card_is_published_with_read_grade(agent: TestClient) -> None:
    card = agent.get("/.well-known/agent-card.json").json()
    skills = {skill["id"]: skill for skill in card["skills"]}
    assert card["name"] == "data"
    assert skills["impact_analysis"]["grade"] == "read"
    assert "orchestrator" in skills["impact_analysis"]["allowed_requesters"]


def test_impact_analysis_returns_numbers_and_evidence(agent: TestClient, tmp_path: Path) -> None:
    """영향 분석 요청 → 결제 실패 건수·영향 고객 수·피해 금액 + 근거."""
    result = _call(agent, tmp_path, "impact_analysis", INCIDENT, "--context-id", INCIDENT_CTX)

    assert result["status"] == "completed"
    assert result["meta"]["context_id"] == INCIDENT_CTX
    assert result["meta"]["requester"] == "orchestrator"  # 토큰에서 꺼낸 값

    output = result["output"]
    assert output["failed_order_count"] == seed.EXPECTED_FAILED_ORDER_COUNT
    assert output["affected_customer_count"] == seed.EXPECTED_AFFECTED_CUSTOMER_COUNT
    assert len(output["affected_customer_ids"]) == output["affected_customer_count"]
    assert output["damage_amount"] == seed.EXPECTED_DAMAGE_AMOUNT
    # 카테고리별 합이 전체와 맞아야 한다 (숫자는 코드가 계산한다)
    assert (
        sum(c["failed_order_count"] for c in output["by_category"])
        == (output["failed_order_count"])
    )
    assert sum(c["damage_amount"] for c in output["by_category"]) == output["damage_amount"]
    # LLM 응답이 없으면 고정 템플릿 문장으로 대체하고 그 사실을 표시한다
    assert result["fallback"] is True
    assert str(seed.EXPECTED_DAMAGE_AMOUNT) in output["summary"].replace(",", "")

    evidence = result["evidence"]
    assert any("shop_failed_orders" in query for query in evidence["queries"])
    assert f"{seed.EXPECTED_FAILED_ORDER_COUNT}건" in evidence["data_summary"]
    assert f"{seed.EXPECTED_DAMAGE_AMOUNT}원" in evidence["data_summary"]


@pytest.mark.parametrize("requester", ["infra", "support", "finance", "planning"])
def test_scenario_a_departments_can_ask(agent: TestClient, tmp_path: Path, requester: str) -> None:
    result = _call(agent, tmp_path, "impact_analysis", INCIDENT, "--as", requester)
    assert result["status"] == "completed"
    assert result["output"]["damage_amount"] == seed.EXPECTED_DAMAGE_AMOUNT


def test_requester_outside_acl_is_rejected(agent: TestClient, tmp_path: Path) -> None:
    result = _call(agent, tmp_path, "impact_analysis", INCIDENT, "--as", "marketing")
    assert result["status"] == "rejected"
    assert result["outcome"] == "FORBIDDEN_SKILL"
