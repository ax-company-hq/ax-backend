"""문의가 진행 중인 사건의 `ctx_inc_`를 이어받는다 (#274): 주문 사실로만, 실패해도 접수는 계속."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events as bus
from ax_contracts.schemas import AgentRequest, AgentResult, ExecuteDecision, TaskStatus
from support_agent import compensation, handlers, incident_context, shop_client
from support_agent.main import app
from support_agent.store import InquiryStore

INCIDENT = "ctx_inc_0001"
FAILED_AT = "2026-10-26T05:00:30Z"


def order(**changes: Any) -> dict[str, Any]:
    base = {
        "order_id": "ord_0001",
        "buyer_id": "buy_0001",
        "status": "payment_failed",
        "total_amount": 23000,
        "items": [],
        "payments": [
            {
                "payment_id": "pay_1",
                "amount": 23000,
                "method_code": "card",
                "status": "failed",
                "failure_code": "PG_UNAVAILABLE",
                "created_at": FAILED_AT,
            }
        ],
    }
    return {**base, **changes}


class World:
    """가짜 shop-api 주문과 가짜 총괄. 마케팅 호출은 따로 기록한다."""

    def __init__(self) -> None:
        self.order: dict[str, Any] | None = order()
        self.shop_down = False
        self.found: dict[str, Any] = {
            "found": True,
            "context_id": INCIDENT,
            "component": "payment",
            "status": "open",
        }
        self.orchestrator_reply: AgentResult | None = None
        self.orchestrator_raises = False
        self.orchestrator_hangs = False
        self.asked: list[AgentRequest] = []
        self.coupons: list[AgentRequest] = []
        self.notice_lookups: list[AgentRequest] = []
        self.notice_gets: list[AgentRequest] = []
        self.notice_views: dict[str, dict[str, Any]] = {}  # get_notice가 아는 공지
        self.executed_contexts: list[str] = []


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("SUPPORT_ORDER_SOURCE", raising=False)
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    monkeypatch.setattr(handlers, "LEDGER", compensation.SequenceLedger())
    monkeypatch.setattr(incident_context, "LOOKUP_TIMEOUT_SECONDS", 0.2)
    bus.reset_mock_bus()
    state = World()

    def shop(request: httpx.Request) -> httpx.Response:
        if state.shop_down:
            raise httpx.ConnectError("refused")
        if state.order is None:
            return httpx.Response(404, json={"error": {"code": "ORDER_NOT_FOUND", "message": "x"}})
        return httpx.Response(200, json={"data": state.order, "meta": {}})

    shop_client.use_client(httpx.AsyncClient(transport=httpx.MockTransport(shop)))

    async def fake_send(target: str, req: AgentRequest) -> AgentResult:
        if target == "orchestrator":
            state.asked.append(req)
            if state.orchestrator_raises:
                raise ConnectionError("down")
            if state.orchestrator_hangs:
                await asyncio.sleep(5)
            if state.orchestrator_reply is not None:
                return state.orchestrator_reply
            return AgentResult(meta=req.meta, status=TaskStatus.COMPLETED, output=state.found)
        assert target == "marketing"
        if req.skill == "get_notice":  # 총괄이 사건에 보관한 공지 번호로 바로 읽는다
            state.notice_gets.append(req)
            view = state.notice_views.get(req.input["notice_id"], {"found": False})
            return AgentResult(meta=req.meta, status=TaskStatus.COMPLETED, output=view)
        if req.skill == "list_active_notices":  # 사건 번호로 들어온 문의는 게시 중인 공지도 찾는다
            state.notice_lookups.append(req)
            return AgentResult(
                meta=req.meta, status=TaskStatus.COMPLETED, output={"items": [], "total": 0}
            )
        assert req.skill == "issue_coupon"
        state.coupons.append(req)
        return AgentResult(
            meta=req.meta, status=TaskStatus.COMPLETED, output={"coupon_id": "cpn_0001"}
        )

    monkeypatch.setattr(handlers, "SEND", fake_send)

    real_execute = handlers.execute

    async def spy_execute(action: str, params: dict, meta: Any, *args: Any, **kwargs: Any):
        state.executed_contexts.append(meta.context_id)
        return await real_execute(action, params, meta, *args, **kwargs)

    monkeypatch.setattr(handlers, "execute", spy_execute)
    yield state
    shop_client.use_client(None)


def call(
    skill: str,
    input: dict[str, Any],
    *,
    context: str = "ctx_ui_0001",
    untrusted: dict[str, Any] | None = None,
    key: str | None = "k-1",
    requester: str = "dashboard-api",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "meta": {
            "context_id": context,
            "token": f"mock:{requester}",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input,
    }
    if untrusted is not None:
        body["untrusted"] = untrusted
    return TestClient(app).post("/a2a", json=body).json()


def create(order_id: str | None = "ord_0001", user_id: int = 1, **kw: Any) -> dict[str, Any]:
    return call(
        "create_inquiry",
        {"user_id": user_id, "customer_name": "김하늘", "order_id": order_id},
        untrusted={"subject": "결제가 안 돼요", "message": "돈이 빠져나간 건 아니죠?"},
        **kw,
    )


def stored_context(result: dict[str, Any]) -> str:
    return handlers.STORE.get(result["output"]["id"]).context_id  # type: ignore[union-attr]


def published_contexts() -> list[str | None]:
    return [e.context_id for e in bus.MOCK_PUBLISHED]


def test_a_payment_server_failure_pins_the_open_incident(world: World) -> None:
    result = create()
    assert result["status"] == "completed"
    assert stored_context(result) == INCIDENT
    (asked,) = world.asked
    assert asked.skill == "find_active_incident"
    assert asked.input == {"component": "payment", "occurred_at": "2026-10-26T05:00:30Z"}
    assert asked.meta.idempotency_key is None
    assert published_contexts() == [INCIDENT]  # 접수 이벤트도 사건 번호로
    assert world.executed_contexts == [INCIDENT]


def test_a_pinned_incident_inquiry_also_looks_up_the_published_notices(world: World) -> None:
    create()
    (lookup,) = world.notice_lookups
    assert lookup.meta.context_id == INCIDENT and lookup.input == {"limit": 3}


def test_an_ordinary_inquiry_does_not_look_up_notices(world: World) -> None:
    world.found = {"found": False}
    create()
    assert world.notice_lookups == []


def test_no_open_incident_keeps_the_ordinary_context(world: World) -> None:
    world.found = {"found": False}
    result = create()
    assert stored_context(result) == "ctx_ui_0001"
    assert len(world.asked) == 1 and published_contexts() == ["ctx_ui_0001"]


def test_the_earliest_server_failure_time_is_used(world: World) -> None:
    world.order = order(
        payments=[
            {
                "payment_id": "pay_2",
                "amount": 1,
                "status": "failed",
                "failure_code": "PG_UNAVAILABLE",
                "created_at": "2026-10-26T05:02:00Z",
            },
            {
                "payment_id": "pay_1",
                "amount": 1,
                "status": "failed",
                "failure_code": "PG_UNAVAILABLE",
                "created_at": "2026-10-26T05:00:10Z",
            },
        ]
    )
    create()
    assert world.asked[0].input["occurred_at"] == "2026-10-26T05:00:10Z"


@pytest.mark.parametrize(
    "changes",
    [
        {
            "payments": [
                {
                    "payment_id": "p",
                    "status": "failed",
                    "failure_code": "PG_DECLINED",
                    "created_at": FAILED_AT,
                }
            ]
        },  # 카드 거절은 사건이 아니다
        {
            "status": "paid",
            "payments": [{"payment_id": "p", "status": "succeeded", "created_at": FAILED_AT}],
        },
        {"status": "pending_payment", "payments": []},
        {"buyer_id": "buy_0002"},  # 남의 주문
    ],
)
def test_without_a_provable_server_failure_the_orchestrator_is_not_asked(
    world: World, changes: dict[str, Any]
) -> None:
    world.order = order(**changes)
    result = create()
    assert world.asked == [] and stored_context(result) == "ctx_ui_0001"


def test_no_order_or_unusable_order_does_not_ask(world: World) -> None:
    assert stored_context(create(order_id=None)) == "ctx_ui_0001"
    assert stored_context(create(order_id="101", key="k-2")) == "ctx_ui_0001"  # 시드 숫자 주문
    world.order = None
    assert stored_context(create(key="k-3")) == "ctx_ui_0001"  # 주문을 찾을 수 없다
    assert world.asked == []


def test_seed_order_source_never_asks(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_ORDER_SOURCE", "seed")
    assert stored_context(create()) == "ctx_ui_0001" and world.asked == []


def test_a_request_that_already_carries_an_incident_is_not_asked_again(world: World) -> None:
    result = create(context="ctx_inc_0009")
    assert world.asked == [] and stored_context(result) == "ctx_inc_0009"


@pytest.mark.parametrize(
    "trouble", ["shop_down", "raises", "hangs", "failed", "garbage", "foreign"]
)
def test_any_trouble_falls_back_to_the_ordinary_context_and_never_blocks(
    world: World, trouble: str
) -> None:
    if trouble == "shop_down":
        world.shop_down = True
    elif trouble == "raises":
        world.orchestrator_raises = True
    elif trouble == "hangs":
        world.orchestrator_hangs = True
    elif trouble == "failed":
        world.orchestrator_reply = AgentResult(
            meta=AgentRequest.model_validate(
                {
                    "meta": {
                        "context_id": "ctx_ui_0001",
                        "token": "t",
                        "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                    },
                    "skill": "find_active_incident",
                }
            ).meta,
            status=TaskStatus.FAILED,
        )
    elif trouble == "garbage":
        world.found = {"found": True}  # context_id 없이 found
    else:
        world.found = {"found": True, "context_id": "ctx_ui_9999"}  # 사건 번호가 아니다
    result = create()
    assert result["status"] == "completed" and stored_context(result) == "ctx_ui_0001"


def test_a_resent_create_does_not_ask_the_orchestrator_again(world: World) -> None:
    create(key="same")
    again = create(key="same")
    assert again["status"] == "completed"
    assert len(world.asked) == 1 and len(handlers.STORE.list()) == 1


def test_follow_up_messages_replies_and_status_use_the_pinned_incident(world: World) -> None:
    inquiry_id = create()["output"]["id"]
    world.executed_contexts.clear()
    bus.reset_mock_bus()
    call(
        "add_customer_message",
        {"inquiry_id": inquiry_id},
        untrusted={"message": "아직도요"},
        key="k-2",
    )
    call("send_reply", {"inquiry_id": inquiry_id}, untrusted={"text": "확인 중입니다"}, key="k-3")
    call("set_inquiry_status", {"inquiry_id": inquiry_id, "status": "resolved"}, key="k-4")
    assert world.executed_contexts == [INCIDENT] * 3
    assert published_contexts() == [INCIDENT] * 2  # 후속 메시지 접수 이벤트 + 해결 이벤트 (#327)
    assert len(world.asked) == 1  # 후속 요청은 다시 묻지 않는다


def test_a_compensation_request_carries_the_incident_and_shares_its_sequence(world: World) -> None:
    inquiry_id = create()["output"]["id"]
    result = call(
        "request_compensation",
        {"inquiry_id": inquiry_id, "buyer_id": "buy_0001", "reason_code": "late_delivery"},
        key=None,
    )
    assert result["status"] == "completed"
    (coupon,) = world.coupons
    assert coupon.meta.context_id == INCIDENT and coupon.sequence == 1
    assert handlers.LEDGER.peek(INCIDENT) == 2  # 사건 번호 하나로 번호가 이어진다


def test_an_ordinary_inquiry_keeps_its_own_context_for_compensation(world: World) -> None:
    world.found = {"found": False}
    inquiry_id = create()["output"]["id"]
    call(
        "request_compensation",
        {"inquiry_id": inquiry_id, "buyer_id": "buy_0001", "reason_code": "late_delivery"},
        key=None,
    )
    assert world.coupons[0].meta.context_id == "ctx_ui_0001"


def test_the_customer_text_never_reaches_the_orchestrator(world: World) -> None:
    create()
    text = str(world.asked[0].model_dump())
    assert "돈이 빠져나간" not in text and "김하늘" not in text


def test_a_held_create_does_not_pin_anything(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def held(*args: Any, **kwargs: Any) -> ExecuteDecision:
        return ExecuteDecision(
            execution_id="exe_1",
            grade="C",
            status="held",
            hold_reason="over_threshold",
            review_id="r",
        )

    monkeypatch.setattr(handlers, "execute", held)
    assert create()["outcome"] == "not_executed_held"
    assert handlers.STORE.list() == []


def test_provable_occurrence_is_a_pure_function_of_the_order() -> None:
    assert incident_context.provable_occurrence(order()) == (
        "payment",
        datetime(2026, 10, 26, 5, 0, 30, tzinfo=UTC),
    )
    assert incident_context.provable_occurrence({"payments": [{"status": "failed"}]}) is None
    assert (
        incident_context.provable_occurrence(
            order(
                payments=[
                    {"status": "failed", "failure_code": "PG_UNAVAILABLE", "created_at": "bad"}
                ]
            )
        )
        is None
    )


def notice_view(notice_id: str, text: str = "결제가 원활하지 않습니다. 복구 중입니다.") -> dict:
    return {
        "found": True,
        "notice_id": notice_id,
        "status": "published",
        "channel_code": "banner",
        "public_text": text,
        "promises": [],
    }


def notice_sources(result: dict[str, Any]) -> list[str]:
    analysis = result["output"]["messages"][0]["analysis"]
    return [s["id"] for s in analysis["sources"] if s["doc_id"] == "notice"]


def test_the_notice_the_orchestrator_kept_for_the_incident_is_read_directly(world: World) -> None:
    world.found = {**world.found, "notice_id": "ntc_0042"}
    world.notice_views["ntc_0042"] = notice_view("ntc_0042")
    result = create()
    assert [g.input for g in world.notice_gets] == [{"notice_id": "ntc_0042"}]
    assert world.notice_lookups == []  # 번호를 알면 게시 중인 공지를 찾지 않는다
    assert notice_sources(result) == ["notice:ntc_0042"]
    assert world.notice_gets[0].meta.context_id == INCIDENT
    drafts = result["output"]["messages"][0]["analysis"]["drafts"]
    assert "notice:ntc_0042" in drafts[0]["source_ids"]


def test_without_a_kept_notice_the_published_notices_are_looked_up(world: World) -> None:
    create()  # 총괄이 notice_id를 안 주면 지금까지처럼 게시 중인 공지를 찾는다
    assert world.notice_gets == [] and len(world.notice_lookups) == 1


def test_a_caller_given_notice_id_wins_over_the_incident_one(world: World) -> None:
    world.found = {**world.found, "notice_id": "ntc_0042"}
    world.notice_views["ntc_0001"] = notice_view("ntc_0001", "호출한 쪽이 준 공지")
    result = call(
        "create_inquiry",
        {"user_id": 1, "customer_name": "김하늘", "order_id": "ord_0001", "notice_id": "ntc_0001"},
        untrusted={"subject": "결제", "message": "안 돼요"},
    )
    assert [g.input for g in world.notice_gets] == [{"notice_id": "ntc_0001"}]
    assert notice_sources(result) == ["notice:ntc_0001"] and world.notice_lookups == []


def test_if_the_kept_notice_cannot_be_read_the_published_ones_are_tried(world: World) -> None:
    world.found = {**world.found, "notice_id": "ntc_0042"}  # get_notice는 이 번호를 모른다
    result = create()
    assert len(world.notice_gets) == 1 and len(world.notice_lookups) == 1
    assert result["status"] == "completed" and notice_sources(result) == []


def test_a_kept_notice_that_is_not_a_notice_number_is_never_used(world: World) -> None:
    world.found = {**world.found, "notice_id": "ntc_1; drop table"}
    create()
    assert world.notice_gets == [] and len(world.notice_lookups) == 1  # 모양이 틀려 무시한다


def test_a_replayed_create_does_not_read_the_notice_again(world: World) -> None:
    world.found = {**world.found, "notice_id": "ntc_0042"}
    world.notice_views["ntc_0042"] = notice_view("ntc_0042")
    create(key="same")
    create(key="same")
    assert len(world.asked) == 1 and len(world.notice_gets) == 1
