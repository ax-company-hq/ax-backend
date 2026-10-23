import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events as bus
from ax_contracts.schemas import (
    AgentRequest,
    EventEnvelope,
    EventType,
    ExecuteDecision,
    RequestMeta,
)
from ax_contracts.schemas.event import EVENT_DATA_MODELS, SupportInquiryReceivedData
from support_agent import events, handlers
from support_agent.main import app
from support_agent.schemas import Analysis
from support_agent.store import InquiryStore

PII_MESSAGE = "배송이 늦어요. 연락처 010-1234-5678, mail me@example.com"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    bus.reset_mock_bus()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def call(
    client: TestClient,
    skill: str,
    input: dict[str, Any],
    untrusted: dict[str, Any],
    key: str,
    context: str = "ctx_req_0001",
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": context,
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input,
        "untrusted": untrusted,
    }
    return client.post("/a2a", json=body).json()


def create(client: TestClient, key: str = "k-1", **input: Any) -> dict[str, Any]:
    return call(
        client,
        "create_inquiry",
        {"user_id": 1, "customer_name": "김하늘", "order_id": 101, **input},
        {"subject": "배송 문의", "message": PII_MESSAGE},
        key,
    )


def _request() -> AgentRequest:
    return AgentRequest(
        meta=RequestMeta(
            context_id="ctx_req_0001",
            token="t",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
        ),
        skill="create_inquiry",
    )


def published() -> list[EventEnvelope]:
    return [e for e in bus.MOCK_PUBLISHED if e.type == EventType.SUPPORT_INQUIRY_RECEIVED]


def test_contract_model_is_registered() -> None:
    assert EVENT_DATA_MODELS[EventType.SUPPORT_INQUIRY_RECEIVED] is SupportInquiryReceivedData


def test_create_inquiry_publishes_one_event_with_facts_only(client: TestClient) -> None:
    result = create(client)
    (event,) = published()
    inquiry_id = result["output"]["id"]
    assert event.source == "support" and event.tenant_id == "shop-demo"
    assert event.context_id == "ctx_req_0001"
    assert event.idempotency_key == f"ctx_req_0001:support.inquiry_received:{inquiry_id}:1"
    data = SupportInquiryReceivedData.model_validate(event.data)
    assert data.inquiry_id == inquiry_id and data.message_count == 1
    assert data.category and data.urgency in {"low", "medium", "high"}
    assert set(event.data) == {"inquiry_id", "order_id", "category", "urgency", "message_count"}


def test_order_id_is_not_invented_from_numeric_seed_id(client: TestClient) -> None:
    create(client, order_id=101, order_no="ORD-2026-0101")
    assert (
        published()[0].data["order_id"] is None
    )  # 계약은 `ord_` 문자열. 숫자 시드 ID를 바꿔 쓰지 않는다


def test_original_text_travels_only_in_untrusted_and_is_masked(client: TestClient) -> None:
    create(client)
    event = published()[0]
    assert event.untrusted["message"] == "배송이 늦어요. 연락처 [전화번호], mail [이메일]"
    dumped = event.model_dump_json()
    assert "010-1234-5678" not in dumped and "me@example.com" not in dumped
    assert "배송이 늦어요" not in str(event.data)


def test_follow_up_customer_message_publishes_next_count(client: TestClient) -> None:
    inquiry_id = create(client)["output"]["id"]
    call(
        client,
        "add_customer_message",
        {"inquiry_id": inquiry_id},
        {"message": "아직도 안 왔어요"},
        "k-2",
    )
    first, second = published()
    assert (first.data["message_count"], second.data["message_count"]) == (1, 2)
    assert first.event_id != second.event_id
    assert second.untrusted["message"] == "아직도 안 왔어요"


def test_retry_with_same_key_does_not_publish_again(client: TestClient) -> None:
    create(client, key="same")
    create(client, key="same")
    assert len(published()) == 1


def test_same_event_id_is_delivered_once_by_the_bus(client: TestClient) -> None:
    # 다른 요청 키여도 같은 사건·문의·메시지 수면 같은 event_id라서 버스가 한 번만 전달한다
    inquiry_id = create(client)["output"]["id"]
    conversation = handlers.STORE.get(inquiry_id)
    assert conversation is not None
    request = _request()
    asyncio.run(events.emit_inquiry_received(request, conversation))
    assert len(published()) == 1


def test_not_executed_write_publishes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def held(*args: Any, **kwargs: Any) -> ExecuteDecision:
        return ExecuteDecision(
            execution_id="exe_1",
            grade="C",
            status="held",
            hold_reason="over_threshold",
            review_id="r",
        )

    monkeypatch.setattr(handlers, "execute", held)
    assert create(client)["outcome"] == "not_executed_held"
    assert published() == []
    assert handlers.STORE.list() == []


def test_other_writes_do_not_publish(client: TestClient) -> None:
    inquiry_id = create(client)["output"]["id"]
    bus.reset_mock_bus()
    call(client, "send_reply", {"inquiry_id": inquiry_id}, {"text": "확인 중입니다"}, "k-r")
    call(client, "set_inquiry_status", {"inquiry_id": inquiry_id, "status": "resolved"}, {}, "k-s")
    assert published() == []


def test_publish_failure_does_not_fail_the_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(envelope: EventEnvelope) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(events, "publish", broken)
    result = create(client)
    assert result["status"] == "completed"
    assert handlers.STORE.get(result["output"]["id"]) is not None


def test_publish_not_connected_is_skipped_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_connected(envelope: EventEnvelope) -> None:
        raise NotImplementedError("TODO: Redis Streams XADD")

    monkeypatch.setattr(events, "publish", not_connected)
    store = InquiryStore()
    conversation = store.create(
        context_id="ctx_req_0001",
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="s",
        order_id=None,
        order_no=None,
        first_message="m",
        analysis=Analysis(),
    )
    request = _request()
    asyncio.run(events.emit_inquiry_received(request, conversation))  # 예외 없이 끝난다
    data = SupportInquiryReceivedData.model_validate(
        events.build_inquiry_received(request, conversation).data
    )
    assert data.category is None and data.urgency is None  # 분석이 없으면 비운다


def test_shop_api_order_id_is_published_as_is(client: TestClient) -> None:
    """shop-api 주문(`ord_…`)은 문의에 문자열로 저장되고 이벤트에 그대로 실린다."""
    result = create(client, order_id="ord_0001")
    assert result["output"]["order_id"] == "ord_0001"
    (event,) = published()
    assert SupportInquiryReceivedData.model_validate(event.data).order_id == "ord_0001"


def test_numeric_order_ids_are_read_as_strings(client: TestClient) -> None:
    """옛 숫자 주문 ID(입력·저장된 기록)는 문자열로 읽는다."""
    from support_agent.schemas import Conversation

    result = create(client, order_id=101)
    assert result["output"]["order_id"] == "101"
    stored = {k: v for k, v in result["output"].items() if k != "execution_id"}
    legacy = Conversation.model_validate({**stored, "order_id": 7})
    assert legacy.order_id == "7"
