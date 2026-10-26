"""공지 번호를 모를 때 `list_active_notices`로 지금 게시 중인 공지를 찾아 답변 근거로 쓴다."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from support_agent import handlers, notices
from support_agent.main import app
from support_agent.schemas import Analysis
from support_agent.store import InquiryStore


def view(n: int, text: str = "결제가 원활하지 않습니다. 복구 중입니다.", **extra: Any) -> dict:
    return {
        "found": True,
        "notice_id": f"ntc_{n:04d}",
        "status": "published",
        "channel_code": "banner",
        "public_text": text,
        "promises": [],
        **extra,
    }


class Bus:
    """마케팅 호출 기록과 응답. `listing`이 `list_active_notices`의 응답이다."""

    def __init__(self) -> None:
        self.sent: list[AgentRequest] = []
        self.listing: dict[str, Any] = {"items": [view(2), view(1)], "total": 2}
        self.status = TaskStatus.COMPLETED
        self.raises = False

    def skills(self) -> list[str]:
        return [r.skill for r in self.sent]


@pytest.fixture(autouse=True)
def bus(monkeypatch: pytest.MonkeyPatch) -> Bus:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    state = Bus()

    async def fake_send(target: str, req: AgentRequest) -> AgentResult:
        assert target == "marketing"
        state.sent.append(req)
        if state.raises:
            raise ConnectionError("down")
        if req.skill == "get_notice":
            return AgentResult(meta=req.meta, status=state.status, output=view(9, "지정한 공지"))
        assert req.skill == "list_active_notices"
        return AgentResult(meta=req.meta, status=state.status, output=state.listing)

    monkeypatch.setattr(handlers, "SEND", fake_send)
    return state


def call(
    skill: str = "create_inquiry",
    context: str = "ctx_inc_0001",
    **fields: Any,
) -> dict[str, Any]:
    if skill == "create_inquiry":
        input_ = {"user_id": 1, "customer_name": "김하늘", **fields}
        untrusted = {"subject": "결제가 안 돼요", "message": "돈이 빠져나간 건 아니죠?"}
    else:
        input_ = {"inquiry_id": 1, **fields}
        untrusted = {"message": "아직도 안 돼요"}
    body = {
        "meta": {
            "context_id": context,
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": f"k-{skill}-{context}",
        },
        "skill": skill,
        "input": input_,
        "untrusted": untrusted,
    }
    return TestClient(app).post("/a2a", json=body).json()


def source_ids(result: dict[str, Any]) -> list[str]:
    analysis = result["output"]["messages"][-1]["analysis"]
    return [s["id"] for s in analysis["sources"] if s["doc_id"] == "notice"]


def seed_inquiry() -> None:
    handlers.STORE.create(
        context_id="ctx_inc_0001",
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="결제",
        order_id=None,
        order_no=None,
        first_message="첫 문의",
        analysis=Analysis(),
    )


def test_an_incident_inquiry_finds_the_published_notices_without_a_notice_id(bus: Bus) -> None:
    result = call()
    assert bus.skills() == ["list_active_notices"]
    assert bus.sent[0].input == {"limit": 3}
    assert bus.sent[0].meta.context_id == "ctx_inc_0001"
    assert bus.sent[0].meta.idempotency_key is None
    assert source_ids(result) == ["notice:ntc_0002", "notice:ntc_0001"]  # 최근 게시 순 그대로
    drafts = result["output"]["messages"][0]["analysis"]["drafts"]
    assert "notice:ntc_0002" in drafts[0]["source_ids"]


def test_an_ordinary_inquiry_does_not_call_marketing(bus: Bus) -> None:
    result = call(context="ctx_inq_0001")
    assert bus.sent == [] and source_ids(result) == []
    assert call(context="ctx_ui_0002")["status"] == "completed" and bus.sent == []


def test_a_component_hint_looks_up_notices_even_outside_an_incident(bus: Bus) -> None:
    result = call(context="ctx_inq_0001", notice_component="payment")
    assert bus.sent[0].input == {"limit": 3, "component": "payment"}
    assert source_ids(result) == ["notice:ntc_0002", "notice:ntc_0001"]


def test_an_explicit_notice_id_wins_and_no_listing_is_requested(bus: Bus) -> None:
    result = call(notice_id="ntc_0009")
    assert bus.skills() == ["get_notice"]
    assert source_ids(result) == ["notice:ntc_0009"]


def test_no_active_notice_gives_no_source_and_the_inquiry_still_completes(bus: Bus) -> None:
    bus.listing = {"items": [], "total": 0, "unknown_reason": "조건에 맞는 게시 중인 공지가 없음"}
    result = call()
    assert result["status"] == "completed" and source_ids(result) == []


@pytest.mark.parametrize("failure", ["failed", "raises", "bad_output"])
def test_marketing_trouble_gives_no_source_and_never_blocks_the_inquiry(
    bus: Bus, failure: str
) -> None:
    if failure == "failed":
        bus.status = TaskStatus.FAILED
    elif failure == "raises":
        bus.raises = True
    else:
        bus.listing = {"items": "not a list"}
    result = call()
    assert result["status"] == "completed" and source_ids(result) == []


def test_at_most_three_notices_are_used_and_duplicates_are_dropped(bus: Bus) -> None:
    bus.listing = {"items": [view(5), view(5), view(4), view(3), view(2), view(1)], "total": 6}
    assert source_ids(call()) == ["notice:ntc_0005", "notice:ntc_0004", "notice:ntc_0003"]


def test_unusable_items_are_skipped(bus: Bus) -> None:
    bus.listing = {"items": ["x", None, {"found": False}, view(7)], "total": 1}
    assert source_ids(call()) == ["notice:ntc_0007"]


def test_an_ended_notice_is_marked_as_ended(bus: Bus) -> None:
    bus.listing = {"items": [view(3, status="ended")], "total": 1}
    result = call()
    analysis = result["output"]["messages"][0]["analysis"]
    text = next(s["text"] for s in analysis["sources"] if s["id"] == "notice:ntc_0003")
    assert "종료된 공지" in text


def test_a_follow_up_message_in_an_incident_also_finds_the_notices(bus: Bus) -> None:
    seed_inquiry()
    result = call("add_customer_message")
    assert bus.skills() == ["list_active_notices"]
    assert source_ids(result) == ["notice:ntc_0002", "notice:ntc_0001"]


def test_the_component_must_be_a_known_one(bus: Bus) -> None:
    result = call(notice_component="billing")
    assert result["status"] == "failed" and bus.sent == []


def test_fetch_active_sources_never_raises_and_respects_the_limit(bus: Bus) -> None:
    import asyncio

    request = AgentRequest.model_validate(
        {
            "meta": {
                "context_id": "ctx_inc_0001",
                "token": "t",
                "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            },
            "skill": "create_inquiry",
        }
    )
    bus.listing = {"items": [view(3), view(2), view(1)], "total": 3}
    found = asyncio.run(notices.fetch_active_sources(request, handlers.SEND, limit=2))
    assert [s.id for s in found] == ["notice:ntc_0003", "notice:ntc_0002"]
    assert bus.sent[0].input == {"limit": 2}
