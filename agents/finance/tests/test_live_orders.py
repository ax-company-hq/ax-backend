"""실제 주문으로 환불 검증: shop-api 주문 조회, 고객지원의 숫자 주문 번호."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_contracts.schemas import RequestMeta, VerifyAmountInput
from finance_agent import handlers
from finance_agent.main import app
from finance_agent.recheck import recompute
from finance_agent.sources import LiveSources, MockSources, order_key, paid_total

META = RequestMeta(
    context_id="ctx_req_1", token="x", deadline=datetime.now(UTC) + timedelta(seconds=30)
)


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("AX_SHOP_API_URL", "http://shop.test")
    events.reset_mock_bus()
    handlers.reset_state(MockSources())


def _shop(handler) -> LiveSources:
    return LiveSources(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


ORDER = {
    "order_id": "ord_7",
    "status": "paid",
    "total_amount": 30000,
    "payments": [
        {"payment_id": "pay_1", "amount": 30000, "status": "failed"},
        {"payment_id": "pay_2", "amount": 30000, "status": "succeeded"},
    ],
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3, "3"),
        ("ord_7", "ord_7"),
        (" ord_7 ", "ord_7"),
        (0, None),
        (-1, None),
        (True, None),
        ("", None),
        (None, None),
    ],
)
def test_order_key(value: object, expected: str | None) -> None:
    assert order_key(value) == expected


def test_paid_total_counts_only_succeeded_payments() -> None:
    assert paid_total(ORDER) == 30000
    assert paid_total({"payments": []}) == 0
    assert paid_total({"payments": [{"status": "succeeded", "amount": "30000"}]}) is None
    assert paid_total({}) is None


@pytest.mark.asyncio
async def test_live_paid_amount_reads_shop_api() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": ORDER, "meta": {}})

    assert await _shop(handler).paid_amount(META, "ord_7") == 30000
    assert seen == ["http://shop.test/api/shop/orders/ord_7"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": {"code": "ORDER_NOT_FOUND", "message": "x"}}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": {"order_id": "ord_7"}}),
    ],
)
async def test_live_paid_amount_unknown_is_none(response: httpx.Response) -> None:
    assert await _shop(lambda request: response).paid_amount(META, "ord_7") is None


@pytest.mark.asyncio
async def test_live_paid_amount_when_shop_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await _shop(handler).paid_amount(META, "ord_7") is None


def _verify(client: TestClient, order_id: object, amount: int) -> dict:
    """환불 다시 계산(`recheck.recompute`, 예전 verify_amount 계산). 캣북스·사후 감사가 쓴다."""
    data = VerifyAmountInput(
        execution_id=f"exe_{order_id}_{amount}",
        action="refund_request",
        requester="support",
        params={"claim_id": 1, "order_id": order_id, "buyer_id": "usr_1", "refund_amount": amount},
        computed_amount=amount,
        threshold_version=1,
    )
    state = handlers.state()
    out = asyncio.run(recompute(META, data, state.sources, state.ledger.refunds))
    return out.model_dump(mode="json")


def test_support_numeric_order_id_is_verified() -> None:
    """고객지원 클레임은 주문 번호를 숫자로 보낸다. 거절하지 않고 같은 주문으로 본다."""
    handlers.reset_state(MockSources({"payments": {"3": 45000}}))
    ok = _verify(TestClient(app), 3, 20000)
    assert (ok["decision"], ok["recomputed"]["paid_amount"]) == ("approved", 45000)
    over = _verify(TestClient(app), 3, 50000)
    assert (over["decision"], over["reason_code"]) == ("rejected", "exceeds_paid")


def test_refund_against_live_shop_order() -> None:
    """실제 모드: shop-api 주문의 성공 결제 금액 안이면 승인, 넘으면 거절."""
    handlers.reset_state(
        _shop(lambda request: httpx.Response(200, json={"data": ORDER, "meta": {}}))
    )
    assert _verify(TestClient(app), "ord_7", 30000)["decision"] == "approved"
    assert _verify(TestClient(app), "ord_7", 30001)["reason_code"] == "exceeds_paid"
