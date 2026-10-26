"""총괄 자신의 `/a2a` skill: `find_active_incident` (#272)와 공지 번호 보관."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_agent_template.config import set_agent_name
from ax_contracts.schemas import AgentRequest, AgentResult, EventEnvelope, EventType, RequestMeta
from ax_orchestrator.main import app, state
from ax_orchestrator.manager import IncidentManager
from ax_orchestrator.plans.scenario_a import scenario_a


def _event(event_id: str, **data) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type=EventType.SHOP_DEGRADED,
        occurred_at=datetime.now(UTC),
        source="shop-api",
        idempotency_key=event_id,
        data={"component": "payment", **data},
    )


def _call(client: TestClient, requester: str, input: dict) -> AgentResult:
    meta = RequestMeta(
        context_id="ctx_req_test",
        token=f"mock:{requester}",
        path=[requester],
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    body = AgentRequest(meta=meta, skill="find_active_incident", input=input)
    response = client.post("/a2a", json=body.model_dump(mode="json"))
    return AgentResult.model_validate(response.json())


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(state, "manager", None)
    yield TestClient(app)
    set_agent_name("orchestrator")


def test_card_lists_find_active_incident(client) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    skill = next(s for s in card["skills"] if s["id"] == "find_active_incident")
    assert skill["grade"] == "read"
    assert skill["allowed_requesters"] == ["support"]


def test_no_manager_means_not_found(client) -> None:
    result = _call(client, "support", {"component": "payment"})
    assert result.status.value == "completed"
    assert result.output["found"] is False
    assert result.output["context_id"] is None


@pytest.mark.asyncio
async def test_open_incident_is_found_by_component(harness, client, monkeypatch) -> None:
    manager = IncidentManager(await harness.start(), scenario_a)
    monkeypatch.setattr(state, "manager", manager)
    inc = await manager.handle(_event("evt_1"))
    result = _call(client, "support", {"component": "payment"})
    assert result.output["found"] is True
    assert result.output["context_id"] == inc.context_id
    assert result.output["status"] == "open"
    await manager.wait(inc.context_id)


@pytest.mark.asyncio
async def test_other_component_is_not_found(harness, client, monkeypatch) -> None:
    manager = IncidentManager(await harness.start(), scenario_a)
    monkeypatch.setattr(state, "manager", manager)
    inc = await manager.handle(_event("evt_1"))
    result = _call(client, "support", {"component": "db"})
    assert result.output["found"] is False
    await manager.wait(inc.context_id)


@pytest.mark.asyncio
async def test_closed_incident_is_found_only_when_time_falls_inside(harness) -> None:
    manager = IncidentManager(await harness.start(), scenario_a)
    inc = await manager.handle(_event("evt_1"))
    await manager.wait(inc.context_id)
    assert inc.status == "resolved" and inc.closed_at is not None
    assert manager.find_active("payment", None) is None  # 시각이 없으면 열린 사건만
    assert manager.find_active("payment", inc.opened_at) is inc
    assert manager.find_active("payment", inc.closed_at + timedelta(minutes=1)) is None
    assert manager.find_active("payment", inc.opened_at - timedelta(minutes=30)) is None


def test_only_support_may_ask(client) -> None:
    result = _call(client, "marketing", {"component": "payment"})
    assert result.status.value == "rejected"
    assert result.outcome == "FORBIDDEN_SKILL"


def test_invalid_input_fails(client) -> None:
    result = _call(client, "support", {"occurred_at": "아님"})
    assert result.status.value == "failed"
    assert result.outcome == "invalid_input"


@pytest.mark.asyncio
async def test_notice_ids_are_kept_on_the_incident(harness) -> None:
    manager = IncidentManager(await harness.start(), scenario_a)
    inc = await manager.handle(_event("evt_1"))
    await manager.wait(inc.context_id)
    assert inc.shared["notice_id"] == inc.outputs["publish_notice"]["notice_id"]
    assert inc.shared["recovered_notice_id"] == inc.outputs["recovered_notice"]["notice_id"]
    assert inc.shared["notice_id"] != inc.shared["recovered_notice_id"]
    assert inc.view()["shared"] == inc.shared


@pytest.mark.asyncio
async def test_missing_notice_id_is_not_invented(harness) -> None:
    harness.fakes.script("marketing", "publish_notice", "forbidden")  # 이관 큐로 넘어가 게시 안 됨
    manager = IncidentManager(await harness.start(), scenario_a)
    inc = await manager.handle(_event("evt_1"))
    await manager.wait(inc.context_id)
    assert inc.steps["publish_notice"].status == "transfer_queued"
    assert "notice_id" not in inc.shared  # 게시되지 않은 공지의 번호는 만들어 내지 않는다
