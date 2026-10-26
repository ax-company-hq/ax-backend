"""`request_compensation(payment_failure)`: 본인 주문에 결제 서버 장애 기록이 있을 때만 요청한다."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from support_agent import compensation, handlers, shop_client
from support_agent.main import app
from support_agent.schemas import Analysis
from support_agent.store import InquiryStore

FAULT = {"payment_id": "pay_1", "status": "failed", "failure_code": "PG_UNAVAILABLE"}
DECLINED = {"payment_id": "pay_2", "status": "failed", "failure_code": "PG_DECLINED"}
PAID = {"payment_id": "pay_3", "status": "succeeded", "failure_code": None}


class World:
    def __init__(self) -> None:
        self.order: dict[str, Any] | None = self.make([FAULT])
        self.down = False
        self.shop_calls = 0
        self.coupons: list[AgentRequest] = []

    @staticmethod
    def make(payments: list[dict[str, Any]], buyer: str = "buy_0001") -> dict[str, Any]:
        return {
            "order_id": "ord_0001",
            "buyer_id": buyer,
            "status": "payment_failed",
            "payments": payments,
        }


@pytest.fixture(autouse=True)
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("SUPPORT_ORDER_SOURCE", raising=False)
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    monkeypatch.setattr(handlers, "LEDGER", compensation.SequenceLedger())
    state = World()

    def shop(request: httpx.Request) -> httpx.Response:
        state.shop_calls += 1
        if state.down:
            raise httpx.ConnectError("refused")
        if state.order is None:
            return httpx.Response(404, json={"error": {"code": "ORDER_NOT_FOUND", "message": "x"}})
        return httpx.Response(200, json={"data": state.order, "meta": {}})

    shop_client.use_client(httpx.AsyncClient(transport=httpx.MockTransport(shop)))

    async def fake_send(target: str, req: AgentRequest) -> AgentResult:
        assert target == "marketing" and req.skill == "issue_coupon"
        state.coupons.append(req)
        return AgentResult(
            meta=req.meta, status=TaskStatus.COMPLETED, output={"coupon_id": "cpn_0001"}
        )

    monkeypatch.setattr(handlers, "SEND", fake_send)
    yield state
    shop_client.use_client(None)


def add_inquiry(order_id: str | None = "ord_0001", user_id: int = 1) -> int:
    return handlers.STORE.create(
        context_id="ctx_inc_0001",
        tenant_id="shop-demo",
        user_id=user_id,
        customer_name="김하늘",
        subject="결제 오류",
        order_id=order_id,
        order_no=None,
        first_message="결제가 안 돼요",
        analysis=Analysis(),
    ).id


def call(
    inquiry_id: int, *, reason: str = "payment_failure", buyer: str = "buy_0001", **fields: Any
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_inc_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "request_compensation",
        "input": {"inquiry_id": inquiry_id, "buyer_id": buyer, "reason_code": reason, **fields},
    }
    return TestClient(app).post("/a2a", json=body).json()


def test_a_server_failed_attempt_on_the_own_order_allows_the_coupon(world: World) -> None:
    result = call(add_inquiry())
    out = result["output"]
    assert result["outcome"] is None and out["eligible"] is True
    assert (out["unit_value"], out["total_value"]) == (2000, 2000)
    assert [c.input["buyer_id"] for c in world.coupons] == ["buy_0001"]
    refs = result["evidence"]["ref_ids"]
    assert "order:ord_0001" in refs and "payment:pay_1" in refs  # 확인한 결제 기록이 근거에 남는다


def test_a_customer_who_paid_after_server_failures_is_still_eligible(world: World) -> None:
    world.order = World.make([FAULT, PAID])
    assert call(add_inquiry())["output"]["eligible"] is True


@pytest.mark.parametrize(
    "payments",
    [[DECLINED], [PAID], [], [{"payment_id": "p", "status": "failed", "failure_code": None}]],
)
def test_without_a_server_failure_there_is_no_coupon(world: World, payments: list) -> None:
    world.order = World.make(payments)
    result = call(add_inquiry())
    assert result["outcome"] == "not_eligible" and result["output"]["eligible"] is False
    assert "결제 서버 문제" in result["output"]["reason"]
    assert world.coupons == []


def test_a_refusal_is_remembered_and_not_asked_again(world: World) -> None:
    world.order = World.make([DECLINED])
    inquiry_id = add_inquiry()
    call(inquiry_id)
    calls = world.shop_calls
    again = call(inquiry_id)
    assert again["output"]["eligible"] is False and world.shop_calls == calls


def test_an_inquiry_without_an_order_gets_no_coupon_and_does_not_ask_the_shop(
    world: World,
) -> None:
    result = call(add_inquiry(order_id=None))
    assert result["outcome"] == "not_eligible" and "주문이 없어" in result["output"]["reason"]
    assert world.shop_calls == 0 and world.coupons == []


def test_seed_orders_cannot_be_verified_so_no_coupon(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPPORT_ORDER_SOURCE", "seed")
    result = call(add_inquiry())
    assert result["outcome"] == "not_eligible" and world.coupons == []


@pytest.mark.parametrize("case", ["other_buyer_order", "other_buyer_param", "missing_order"])
def test_someone_elses_order_or_buyer_is_refused_without_saying_why(
    world: World, case: str
) -> None:
    if case == "other_buyer_order":
        world.order = World.make([FAULT], buyer="buy_0002")
    elif case == "missing_order":
        world.order = None
    result = call(add_inquiry(), buyer="buy_0002" if case == "other_buyer_param" else "buy_0001")
    assert result["outcome"] == "not_eligible"
    assert result["output"]["reason"] == "주문을 확인할 수 없다"
    assert world.coupons == []


def test_when_the_shop_is_down_it_fails_and_the_request_can_be_repeated(world: World) -> None:
    inquiry_id = add_inquiry()
    world.down = True
    failed = call(inquiry_id)
    assert failed["status"] == "failed" and failed["outcome"] == "shop_unavailable"
    assert world.coupons == []
    world.down = False  # 복구되면 같은 요청이 다시 통과한다 (실패는 저장하지 않았다)
    assert call(inquiry_id)["output"]["eligible"] is True


def test_other_reasons_are_not_checked_against_the_order(world: World) -> None:
    result = call(add_inquiry(order_id=None), reason="late_delivery")
    assert result["output"]["eligible"] is True and world.shop_calls == 0


def test_a_policy_refusal_comes_before_any_shop_call(world: World) -> None:
    result = call(add_inquiry(), count=9)
    assert result["output"]["eligible"] is False and world.shop_calls == 0


def test_the_incident_wide_compensation_is_not_affected(world: World) -> None:
    world.order = None  # 문의 경로 검사와 무관하게 총괄이 준 영향 고객 목록을 따른다
    body = {
        "meta": {
            "context_id": "ctx_inc_0001",
            "token": "mock:orchestrator",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "request_incident_compensation",
        "input": {"buyer_ids": ["buy_0007", "buy_0008"]},
    }
    out = TestClient(app).post("/a2a", json=body).json()["output"]
    assert out["requested"] == 2 and world.shop_calls == 0
