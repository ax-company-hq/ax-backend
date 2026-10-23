import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import EventEnvelope, EventType
from ax_orchestrator.main import app, state
from ax_orchestrator.manager import IncidentManager
from ax_orchestrator.plans.scenario_a import scenario_a


def event(event_id: str, type_: EventType, **data) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type=type_,
        occurred_at=datetime.now(UTC),
        source="shop-api",
        idempotency_key=event_id,
        data={"component": "payment", **data},
    )


@pytest.mark.asyncio
async def test_event_opens_incident_with_context_id(harness) -> None:
    mgr = IncidentManager(await harness.start(), scenario_a)
    inc = await mgr.handle(event("evt_1", EventType.SHOP_DEGRADED, metric_code="error_rate_5xx"))
    assert inc.context_id.startswith("ctx_inc_")
    await mgr.wait(inc.context_id)
    assert inc.status == "resolved"


@pytest.mark.asyncio
async def test_same_event_twice_is_one_incident(harness) -> None:
    mgr = IncidentManager(await harness.start(), scenario_a)
    first = await mgr.handle(event("evt_1", EventType.SHOP_DEGRADED))
    assert await mgr.handle(event("evt_1", EventType.SHOP_DEGRADED)) is None
    await mgr.wait(first.context_id)
    assert len(mgr.incidents) == 1


@pytest.mark.asyncio
async def test_same_component_open_incident_is_merged(harness) -> None:
    harness.fakes.latency = 0.05
    mgr = IncidentManager(await harness.start(), scenario_a)
    first = await mgr.handle(event("evt_1", EventType.SHOP_DEGRADED))
    merged = await mgr.handle(event("evt_2", EventType.INFRA_ANOMALY_DETECTED))
    assert merged is first
    assert any("합침" in e["message"] for e in first.events)
    await mgr.wait(first.context_id)
    assert len(mgr.incidents) == 1


@pytest.mark.asyncio
async def test_low_severity_anomaly_and_inquiries_are_not_incidents(harness) -> None:
    mgr = IncidentManager(await harness.start(), scenario_a)
    assert await mgr.handle(event("evt_1", EventType.METRIC_ANOMALY, severity="low")) is None
    assert await mgr.handle(event("evt_2", EventType.SUPPORT_INQUIRY_RECEIVED)) is None
    assert not mgr.incidents
    inc = await mgr.handle(event("evt_3", EventType.METRIC_ANOMALY, severity="high"))
    assert inc is not None
    await mgr.wait(inc.context_id)


def test_demo_api_runs_scenario_and_hold_decision(monkeypatch) -> None:
    monkeypatch.setattr(state, "mock", True)
    monkeypatch.setattr(state, "latency", 0.0)
    monkeypatch.setattr(state, "hold_timeout", 5.0)
    monkeypatch.setattr(state, "manager", None)
    with TestClient(app) as client:
        created = client.post("/api/demo/incidents", json={"hold_rollback": True}).json()["data"]
        ctx = created["context_id"]
        for _ in range(200):
            data = client.get(f"/api/incidents/{ctx}").json()["data"]
            if data["pending_holds"]:
                break
            time.sleep(0.01)
        assert data["pending_holds"][0]["step_id"] == "rollback"
        ok = client.post(f"/api/incidents/{ctx}/holds/rollback", json={"approved": True})
        assert ok.status_code == 200
        for _ in range(200):
            data = client.get(f"/api/incidents/{ctx}").json()["data"]
            if data["status"] != "open":
                break
            time.sleep(0.01)
        assert data["status"] == "resolved"
        assert client.get("/demo").status_code == 200
        assert client.get("/api/registry").json()["data"]["depts"]
