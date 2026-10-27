"""재결제 규칙과 동시 결제 안전(#326 요청 3), 실패 코드 유지(요청 4)를 고정하는 회귀 테스트."""

import threading
import time

import pytest

from ax_agent_template import events
from ax_contracts.schemas import EventEnvelope, EventType, PaymentFailedData
from ax_shop_api.db import Db
from ax_shop_api.seed import seed
from ax_shop_api.service import Gateway, PaymentOutcome, Shop, ShopError


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    events.reset_mock_bus()


class SlowGateway(Gateway):
    """결제 판정이 오래 걸리는 경우를 흉내 낸다(상태 확인과 변경 사이에 틈이 있으면 드러난다)."""

    def attempt(self, simulate: str | None) -> PaymentOutcome:
        time.sleep(0.05)
        return super().attempt(simulate)


def _shop(gateway: Gateway | None = None) -> tuple[Shop, str]:
    db = Db(":memory:")
    seed(db)
    shop = Shop(db, gateway)
    order = shop.create_order("buy_0001", [("prd_0001", 1)])
    return shop, order["order_id"]


def _outbox(shop: Shop) -> list[EventEnvelope]:
    """저장된 이벤트(발행 전). 발행은 API 계층의 `outbox.flush`가 한다."""
    with shop.db.tx() as conn:
        rows = conn.execute("SELECT payload FROM outbox_events ORDER BY rowid").fetchall()
    return [EventEnvelope.model_validate_json(r["payload"]) for r in rows]


def test_failed_order_can_be_retried_until_it_succeeds() -> None:
    shop, order_id = _shop()
    first = shop.pay(order_id, "card", "failure")
    second = shop.pay(order_id, "card", "failure")
    assert first["status"] == second["status"] == "payment_failed"
    paid = shop.pay(order_id, "card", None)
    assert paid["status"] == "paid"
    assert [p["status"] for p in paid["payments"]] == ["failed", "failed", "succeeded"]
    assert len({p["payment_id"] for p in paid["payments"]}) == 3  # 시도마다 새 결제 기록


def test_concurrent_payments_leave_exactly_one_success() -> None:
    shop, order_id = _shop(SlowGateway())
    barrier = threading.Barrier(2)
    results: list[object] = []

    def attempt() -> None:
        barrier.wait()
        try:
            results.append(shop.pay(order_id, "card", None))
        except ShopError as error:
            results.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors = [r for r in results if isinstance(r, ShopError)]
    assert len(errors) == 1
    assert (errors[0].code, errors[0].status) == ("ORDER_NOT_PAYABLE", 409)
    order = shop.get_order(order_id)
    assert order["status"] == "paid"
    assert [p["status"] for p in order["payments"]] == ["succeeded"]
    assert [e.type for e in _outbox(shop)].count(EventType.PAYMENT_SUCCEEDED) == 1


def test_concurrent_retry_after_failure_still_one_success() -> None:
    shop, order_id = _shop(SlowGateway())
    shop.pay(order_id, "card", "failure")
    barrier = threading.Barrier(3)
    codes: list[str] = []

    def attempt() -> None:
        barrier.wait()
        try:
            shop.pay(order_id, "card", None)
            codes.append("ok")
        except ShopError as error:
            codes.append(error.code)

    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == ["ORDER_NOT_PAYABLE", "ORDER_NOT_PAYABLE", "ok"]
    statuses = [p["status"] for p in shop.get_order(order_id)["payments"]]
    assert statuses == ["failed", "succeeded"]


def test_payment_after_success_is_rejected_without_new_attempt() -> None:
    shop, order_id = _shop()
    shop.pay(order_id, "card", None)
    with pytest.raises(ShopError) as caught:
        shop.pay(order_id, "card", None)
    assert caught.value.code == "ORDER_NOT_PAYABLE"
    assert len(shop.get_order(order_id)["payments"]) == 1


@pytest.mark.parametrize(
    ("inject", "simulate", "code"),
    [(True, None, "PG_UNAVAILABLE"), (False, "failure", "PG_DECLINED")],
)
def test_failure_code_is_kept_in_response_and_event(
    inject: bool, simulate: str | None, code: str
) -> None:
    shop, order_id = _shop()
    if inject:
        shop.gateway.inject("failure")
    order = shop.pay(order_id, "card", simulate)
    assert order["status"] == "payment_failed"
    assert order["payments"][-1]["failure_code"] == code  # 응답(주문의 결제 목록)
    event = _outbox(shop)[-1]
    assert event.type == EventType.PAYMENT_FAILED
    data = PaymentFailedData.model_validate(event.data)  # 이벤트
    assert (data.failure_code, data.order_id) == (code, order_id)
    assert data.payment_id == order["payments"][-1]["payment_id"]
