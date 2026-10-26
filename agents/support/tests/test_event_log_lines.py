"""발행한 이벤트가 공통 로그에 `event_published` 줄로 남는다 (대시보드 소통 탭의 이벤트 줄)."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import common_log
from ax_agent_template import events as bus
from ax_contracts.data_access.common_log import CommonLogQuery, LogRecord
from ax_contracts.schemas import AgentRequest, EventEnvelope, RequestMeta
from support_agent import events, handlers
from support_agent.main import app
from support_agent.schemas import Analysis
from support_agent.store import InquiryStore

CTX = "ctx_ui_0001"
PII_MESSAGE = "배송이 늦어요. 연락처 010-1234-5678, mail me@example.com"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    bus.reset_mock_bus()
    events._logged.clear()
    common_log.set_store(common_log.SqliteLogStore(str(tmp_path / "common.db")))
    yield
    common_log.reset_store()
    events._logged.clear()


def create(key: str = "k-1") -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": CTX,
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": "create_inquiry",
        "input": {"user_id": 1, "customer_name": "김하늘", "order_id": "ord_0001"},
        "untrusted": {"subject": "배송 문의", "message": PII_MESSAGE},
    }
    return TestClient(app).post("/a2a", json=body).json()


def event_lines() -> list[LogRecord]:
    store = common_log.get_store()
    assert store is not None
    return store.query(CommonLogQuery(kinds=["event_published"])).records


def test_publishing_an_event_leaves_one_event_published_line() -> None:
    create()
    (line,) = event_lines()
    (sent,) = bus.MOCK_PUBLISHED
    assert line.kind == "event_published" and line.source == "support"
    assert line.context_id == CTX and line.tenant_id == "shop-demo"
    assert line.payload == {"event_type": "support.inquiry_received", "event_id": sent.event_id}


def test_the_line_never_carries_the_customer_text_or_event_data() -> None:
    create()
    text = str(event_lines()[0].model_dump())
    for leaked in ("010-1234-5678", "me@example.com", "배송이 늦어요", "ord_0001", "김하늘"):
        assert leaked not in text


def test_each_follow_up_message_is_its_own_line() -> None:
    inquiry_id = create()["output"]["id"]
    body = {
        "meta": {
            "context_id": CTX,
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": "k-2",
        },
        "skill": "add_customer_message",
        "input": {"inquiry_id": inquiry_id},
        "untrusted": {"message": "아직도 안 왔어요"},
    }
    TestClient(app).post("/a2a", json=body)
    ids = [line.payload["event_id"] for line in event_lines()]
    assert len(ids) == 2 and ids[0] != ids[1]


def request() -> AgentRequest:
    return AgentRequest(
        meta=RequestMeta(
            context_id=CTX, token="t", deadline=datetime.now(UTC) + timedelta(seconds=30)
        ),
        skill="create_inquiry",
    )


def conversation_for_emit() -> Any:
    return InquiryStore().create(
        context_id=CTX,
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="s",
        order_id=None,
        order_no=None,
        first_message="m",
        analysis=Analysis(),
    )


def test_sending_the_same_event_again_does_not_add_a_second_line() -> None:
    conversation = conversation_for_emit()
    asyncio.run(events.emit_inquiry_received(request(), conversation))
    asyncio.run(events.emit_inquiry_received(request(), conversation))
    assert len(bus.MOCK_PUBLISHED) == 1 and len(event_lines()) == 1


def test_an_event_that_was_not_published_leaves_no_line(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_connected(envelope: EventEnvelope) -> None:
        raise NotImplementedError("TODO: Redis Streams XADD")

    monkeypatch.setattr(events, "publish", not_connected)
    asyncio.run(events.emit_inquiry_received(request(), conversation_for_emit()))
    assert event_lines() == []


def test_a_failed_publish_leaves_no_line_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(envelope: EventEnvelope) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "publish", broken)
    asyncio.run(events.emit_inquiry_received(request(), conversation_for_emit()))
    assert event_lines() == []


def test_without_a_common_log_store_publishing_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    common_log.reset_store()
    monkeypatch.delenv(common_log.DB_ENV, raising=False)
    assert create()["status"] == "completed"
    assert len(bus.MOCK_PUBLISHED) == 1
