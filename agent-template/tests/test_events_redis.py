"""이벤트 버스 실제 모드(Redis Streams): fakeredis로 발행·구독·ack·재전달·죽은 편지함을 본다."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import fakeredis
import pytest
import pytest_asyncio

from ax_agent_template import events
from ax_agent_template.config import set_agent_name
from ax_contracts.schemas import EventEnvelope, EventType


@pytest_asyncio.fixture(autouse=True)
async def real_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.delenv("FINANCE_MOCK", raising=False)
    monkeypatch.setenv("AX_EVENT_STREAM", "ax:test-events")
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    events.set_redis_client(client)
    set_agent_name("orchestrator")
    yield client
    await events.stop_consumer()
    await client.aclose()  # 닫지 않으면 fakeredis 소켓 스레드가 남아 테스트가 끝나지 않는다


def make(event_id: str, type_: EventType = EventType.METRIC_ANOMALY) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        type=type_,
        occurred_at=datetime(2026, 10, 26, 5, 0, tzinfo=UTC),
        source="data",
        idempotency_key=event_id,
        data={"metric": "payment_failure_rate", "severity": "high"},
    )


async def stream_len(client, name: str = "ax:test-events") -> int:
    return await client.xlen(name)


@pytest.mark.asyncio
async def test_published_event_reaches_a_subscriber_and_is_acked(real_mode) -> None:
    got: list[str] = []

    async def handler(env: EventEnvelope) -> None:
        got.append(env.event_id)

    events.subscribe([EventType.METRIC_ANOMALY], handler)
    await events.publish(make("evt_1"))
    assert await events.poll_once() == 1
    assert got == ["evt_1"]
    pending = await real_mode.xpending("ax:test-events", "orchestrator")
    assert pending["pending"] == 0  # 끝난 건은 ack됐다
    assert await events.poll_once() == 0  # 다시 받지 않는다


@pytest.mark.asyncio
async def test_same_event_id_is_published_once(real_mode) -> None:
    await events.publish(make("evt_dup"))
    await events.publish(make("evt_dup"))
    assert await stream_len(real_mode) == 1


@pytest.mark.asyncio
async def test_only_subscribed_types_reach_the_handler(real_mode) -> None:
    got: list[str] = []

    async def handler(env: EventEnvelope) -> None:
        got.append(env.type.value)

    events.subscribe([EventType.SHOP_DEGRADED], handler)
    await events.publish(make("evt_a", EventType.METRIC_ANOMALY))
    await events.publish(make("evt_b", EventType.SHOP_DEGRADED))
    await events.poll_once()
    assert got == ["shop.degraded"]
    pending = await real_mode.xpending("ax:test-events", "orchestrator")
    assert pending["pending"] == 0  # 관심 없는 건도 ack해서 쌓이지 않는다


@pytest.mark.asyncio
async def test_a_failed_handler_leaves_the_event_to_be_received_again(real_mode) -> None:
    calls: list[str] = []
    fail = {"on": True}

    async def handler(env: EventEnvelope) -> None:
        calls.append(env.event_id)
        if fail["on"]:
            raise RuntimeError("처리 실패")

    events.subscribe([EventType.METRIC_ANOMALY], handler)
    await events.publish(make("evt_retry"))
    await events.poll_once()
    pending = await real_mode.xpending("ax:test-events", "orchestrator")
    assert pending["pending"] == 1  # ack하지 않았다
    fail["on"] = False
    await events.poll_once()  # 내 몫으로 남은 건을 다시 받는다
    assert calls == ["evt_retry", "evt_retry"]
    pending = await real_mode.xpending("ax:test-events", "orchestrator")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_events_missed_while_down_are_handled_after_restart(real_mode) -> None:
    await events.publish(make("evt_before_group"))  # 소비자가 한 번도 안 읽은 상태
    got: list[str] = []

    async def handler(env: EventEnvelope) -> None:
        got.append(env.event_id)

    events.subscribe([EventType.METRIC_ANOMALY], handler)
    await events.poll_once()  # 그룹이 처음 만들어져도 처음부터 읽는다
    assert got == ["evt_before_group"]


@pytest.mark.asyncio
async def test_an_event_that_keeps_failing_goes_to_the_dead_letter_stream(real_mode) -> None:
    async def handler(env: EventEnvelope) -> None:
        raise RuntimeError("계속 실패")

    events.subscribe([EventType.METRIC_ANOMALY], handler)
    await events.publish(make("evt_poison"))
    for _ in range(events.MAX_DELIVERIES + 1):
        await events.poll_once()
    assert await stream_len(real_mode, "ax:test-events:dead") == 1
    pending = await real_mode.xpending("ax:test-events", "orchestrator")
    assert pending["pending"] == 0  # 더 이상 붙잡지 않는다


@pytest.mark.asyncio
async def test_each_service_gets_its_own_copy(real_mode) -> None:
    got: dict[str, list[str]] = {"orchestrator": [], "finance": []}

    def handler_for(name: str):
        async def handler(env: EventEnvelope) -> None:
            got[name].append(env.event_id)

        return handler

    await events.publish(make("evt_all"))
    for name in ("orchestrator", "finance"):
        events._subscribers.clear()
        set_agent_name(name)
        events.subscribe([EventType.METRIC_ANOMALY], handler_for(name))
        await events.poll_once()
    assert got == {"orchestrator": ["evt_all"], "finance": ["evt_all"]}


@pytest.mark.asyncio
async def test_background_consumer_handles_events_and_stops_cleanly(real_mode, monkeypatch) -> None:
    monkeypatch.setattr(events, "BLOCK_MS", 0)  # fakeredis는 막고 기다리는 읽기를 못 쓴다
    got = asyncio.Event()

    async def handler(env: EventEnvelope) -> None:
        got.set()

    events.subscribe([EventType.METRIC_ANOMALY], handler)
    task = events.start_consumer()
    assert task is not None
    await events.publish(make("evt_bg"))
    await asyncio.wait_for(got.wait(), timeout=5)
    await events.stop_consumer()
    assert task.done()


def test_real_mode_without_redis_url_raises_a_not_implemented_subclass(monkeypatch) -> None:
    events.set_redis_client(None)
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(NotImplementedError):  # 기존 호출부(finance)의 except가 그대로 받는다
        events.subscribe([EventType.METRIC_ANOMALY], lambda env: None)  # type: ignore[arg-type,return-value]
