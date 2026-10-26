"""총괄의 이벤트 버스 구독 (ORC-02): 버스에 올라온 이벤트로 사건이 열린다."""

from datetime import UTC, datetime

import fakeredis
import pytest
import pytest_asyncio

from ax_agent_template import events
from ax_agent_template.config import set_agent_name
from ax_contracts.schemas import EventEnvelope, EventType
from ax_orchestrator.main import start_event_bus, state


@pytest_asyncio.fixture
async def bus(monkeypatch):
    for name in ("ORCHESTRATOR_MOCK", "AGENT_TEMPLATE_MOCK", "ORCHESTRATOR_REAL_DEPTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AX_EVENT_STREAM", "ax:test-orch")
    monkeypatch.setattr(state, "mock", False)
    monkeypatch.setattr(state, "manager", None)
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    events.set_redis_client(client)
    yield client
    await events.stop_consumer()
    await client.aclose()  # 닫지 않으면 fakeredis 소켓 스레드가 남는다
    events.reset_mock_bus()
    set_agent_name("orchestrator")


def metric_anomaly(event_id: str, severity: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type=EventType.METRIC_ANOMALY,
        occurred_at=datetime.now(UTC),
        source="data",
        idempotency_key=event_id,
        data={"component": "payment", "metric": "payment_failure_rate", "severity": severity},
    )


@pytest.mark.asyncio
async def test_metric_anomaly_on_the_bus_opens_an_incident(bus) -> None:
    assert await start_event_bus() is True
    await events.stop_consumer()  # 테스트에서는 직접 한 번씩 읽는다(막고 기다리는 읽기를 피함)
    await events.publish(metric_anomaly("evt_bus_1", "high"))
    assert await events.poll_once() == 1
    incidents = list(state.manager.incidents.values())
    assert len(incidents) == 1 and incidents[0].component == "payment"
    await state.manager.wait(incidents[0].context_id)


@pytest.mark.asyncio
async def test_low_severity_is_only_noted_and_duplicates_are_ignored(bus) -> None:
    await start_event_bus()
    await events.stop_consumer()
    await events.publish(metric_anomaly("evt_bus_2", "low"))
    await events.publish(metric_anomaly("evt_bus_2", "low"))  # 같은 event_id는 한 번만 쌓인다
    assert await events.poll_once() == 1
    assert state.manager.incidents == {}  # 심각도가 낮으면 사건을 열지 않는다


@pytest.mark.asyncio
async def test_no_bus_in_mock_mode(monkeypatch) -> None:
    monkeypatch.setattr(state, "mock", True)
    assert await start_event_bus() is False
