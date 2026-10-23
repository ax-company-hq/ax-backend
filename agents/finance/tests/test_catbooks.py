"""캣북스 연결: 클레임을 읽어서 재무 규칙으로 확인 (네트워크 없이 가짜 캣북스로)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from finance_agent import catbooks, handlers
from finance_agent.main import app
from finance_agent.sources import MockSources

BASE = "http://catbooks.test"


def _claim(cid: int, order_id: int, **kw: Any) -> dict[str, Any]:
    row = {
        "id": cid,
        "order_id": order_id,
        "order_no": f"2026-{order_id}",
        "customer": f"고객{cid}",
        "type": "return",
        "reason": "defect",
        "status": "awaiting_return",
        "created_at": f"2026-10-2{cid % 10}T00:00:00+00:00",
        "lines": [{"order_item_id": order_id * 10, "title": "책", "qty": 1, "refund_cash": 20000}],
        "deductions": [],
        "expected_refund": 20000,
        "points_back": 0,
        "compensation": 2000,
        "payable": 20000,
        "refund": 0,
        "abuse": {"score": 0},
        "manager_approved": False,
    }
    return row | kw


def _receipt(order_id: int, paid: int, item_paid: int | None = None, qty: int = 1) -> dict:
    return {
        "paid_amount": paid,
        "shipping_fee": 0,
        "items": [{"id": order_id * 10, "qty": qty, "paid_amount": item_paid or 20000}],
    }


def _fake(claims: list[dict], receipts: dict[int, dict], queue: list[dict] | None = None):
    """가짜 캣북스. GET 말고 다른 요청이 오면 테스트를 실패시킨다(읽기 전용 확인)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "재무 AI는 캣북스에 GET만 보낸다"
        path = request.url.path
        seen.append(path)
        if path == "/api/admin/claims":
            return httpx.Response(200, json=claims)
        if path == "/api/admin/queue":
            return httpx.Response(200, json={"queue": queue or [], "awaiting_return": []})
        if path.startswith("/api/admin/claims/"):
            cid = int(path.rsplit("/", 1)[1])
            order_id = next(c["order_id"] for c in claims if c["id"] == cid)
            if order_id not in receipts:
                return httpx.Response(404, json={"detail": "x"})
            return httpx.Response(200, json={"id": cid, "receipt": receipts[order_id]})
        return httpx.Response(404)

    client = catbooks.CatbooksClient(
        BASE, httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return client, seen


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("FINANCE_CATBOOKS_POLL_SECONDS", "0")
    events.reset_mock_bus()
    handlers.reset_state(MockSources())


async def _sync(claims: list[dict], receipts: dict[int, dict], queue: list[dict] | None = None):
    client, seen = _fake(claims, receipts, queue)
    summary = await catbooks.sync(client)
    items = {i["claim_id"]: i for i in catbooks.items_newest_first()}
    return summary, items, seen


@pytest.mark.asyncio
async def test_small_refund_within_paid_passes_with_evidence() -> None:
    queue = [{"claim_id": 1, "position": 1, "ready": True}]
    summary, items, _ = await _sync([_claim(1, 10)], {10: _receipt(10, 25000)}, queue)
    item = items[1]
    assert summary["ok"] and summary["counts"] == {"pass": 1, "blocked": 0, "needs_human": 0}
    assert (item["result"], item["result_ko"], item["reason_code"]) == (
        "pass",
        "통과",
        "recomputed_match",
    )
    assert (item["paid_amount"], item["refunded_before"], item["refund_amount"]) == (
        25000,
        0,
        20000,
    )
    assert item["queue_position"] == 1 and item["queue_ready"] is True
    assert "주문 2026-10" in item["explanation"] and "20,000원" in item["explanation"]
    assert item["needs_safety"] is False


@pytest.mark.asyncio
async def test_refund_over_paid_minus_already_refunded_is_blocked() -> None:
    done = _claim(2, 20, status="completed", refund=10000)
    claim = _claim(3, 20)
    _, items, _ = await _sync([claim, done], {20: _receipt(20, 25000, item_paid=20000)})
    assert set(items) == {3}  # 완료된 건은 목록에 없고, 이미 환불한 금액으로만 쓴다
    item = items[3]
    assert (item["result"], item["reason_code"]) == ("blocked", "exceeds_paid")
    assert item["refunded_before"] == 10000
    assert "이미 10,000원" in item["explanation"]


@pytest.mark.asyncio
async def test_refund_that_does_not_match_the_rule_is_blocked() -> None:
    claim = _claim(4, 30, expected_refund=24000, payable=24000)
    _, items, _ = await _sync([claim], {30: _receipt(30, 50000, item_paid=20000)})
    assert (items[4]["result"], items[4]["reason_code"]) == ("blocked", "amount_mismatch")
    assert "규정 계산과 다릅니다" in items[4]["explanation"]


@pytest.mark.asyncio
async def test_line_refund_must_follow_paid_share() -> None:
    line = [{"order_item_id": 400, "title": "책", "qty": 1, "refund_cash": 15000}]
    claim = _claim(5, 40, lines=line, expected_refund=15000, payable=15000)
    _, items, _ = await _sync([claim], {40: _receipt(40, 20000, item_paid=20000, qty=2)})
    assert items[5]["reason_code"] == "amount_mismatch"
    assert "10,000원" in items[5]["explanation"]


@pytest.mark.asyncio
async def test_deductions_and_shipping_are_recomputed() -> None:
    line = [{"order_item_id": 500, "title": "책", "qty": 1, "refund_cash": 12000}]
    deduction = [{"code": "RETURN_FEE", "amount": 3000}]
    mind = _claim(
        6, 50, reason="change_of_mind", lines=line, deductions=deduction, compensation=0
    ) | {"expected_refund": 9000, "payable": 9000}
    receipt = _receipt(50, 14500, item_paid=12000) | {"shipping_fee": 2500}
    _, items, _ = await _sync([mind], {50: receipt})
    assert items[6]["result"] == "pass"
    cancel = _claim(7, 50, type="cancel", reason="change_of_mind", lines=line, compensation=0)
    cancel |= {"expected_refund": 14500, "payable": 14500}  # 전체 취소는 배송비까지
    _, items, _ = await _sync([cancel], {50: receipt})
    assert items[7]["result"] == "pass", items[7]["explanation"]


@pytest.mark.asyncio
async def test_big_refund_within_ai_limit_passes_but_must_go_through_safety() -> None:
    line = [{"order_item_id": 600, "title": "전집", "qty": 1, "refund_cash": 135000}]
    claim = _claim(8, 60, lines=line, expected_refund=135000, payable=135000)
    _, items, _ = await _sync([claim], {60: _receipt(60, 135000, item_paid=135000)})
    item = items[8]
    assert (item["result"], item["reason_code"], item["needs_safety"]) == (
        "pass",
        "within_ai_limit",
        True,
    )
    assert "안전 장치" in item["explanation"] and "catbooks_order" not in item["explanation"]


@pytest.mark.asyncio
async def test_refund_over_ai_limit_needs_a_person() -> None:
    line = [{"order_item_id": 700, "title": "전집", "qty": 1, "refund_cash": 350000}]
    claim = _claim(9, 70, lines=line, expected_refund=350000, payable=350000)
    _, items, _ = await _sync([claim], {70: _receipt(70, 400000, item_paid=350000)})
    assert (items[9]["result"], items[9]["result_ko"], items[9]["reason_code"]) == (
        "needs_human",
        "사람 확인 필요",
        "over_ai_limit",
    )


@pytest.mark.asyncio
async def test_abuse_flag_and_undecided_compensation_need_a_person() -> None:
    abuse = _claim(11, 80, status="needs_review", abuse={"score": 60})
    missing = _claim(
        12, 81, type="compensation", reason="missing_item", status="needs_review", lines=[]
    ) | {"expected_refund": 0, "payable": 0, "compensation": 0}
    receipts = {80: _receipt(80, 30000, item_paid=20000), 81: _receipt(81, 10000)}
    summary, items, _ = await _sync([abuse, missing], receipts)
    assert items[11]["reason_code"] == "abuse_suspected"
    assert items[12]["reason_code"] == "amount_undecided"
    assert summary["counts"]["needs_human"] == 2


@pytest.mark.asyncio
async def test_exchange_and_compensation_points_follow_the_rule() -> None:
    line = [{"order_item_id": 900, "title": "티셔츠", "qty": 1, "refund_cash": 0}]
    ok = _claim(13, 90, type="exchange", status="return_received", lines=line)
    ok |= {"expected_refund": 0, "payable": 0}
    wrong = _claim(14, 91, type="exchange", lines=line, compensation=5000)
    wrong |= {"expected_refund": 0, "payable": 0, "lines": [line[0] | {"order_item_id": 910}]}
    receipts = {90: _receipt(90, 20000), 91: _receipt(91, 20000)}
    _, items, _ = await _sync([ok, wrong], receipts)
    assert (items[13]["result"], items[13]["reason_code"]) == ("pass", "no_cash_refund")
    assert (items[14]["result"], items[14]["reason_code"]) == ("blocked", "amount_mismatch")
    assert "보상 적립금 5,000원" in items[14]["explanation"]


@pytest.mark.asyncio
async def test_missing_receipt_needs_a_person() -> None:
    _, items, _ = await _sync([_claim(15, 95)], {})
    assert (items[15]["result"], items[15]["reason_code"]) == ("needs_human", "no_receipt")


@pytest.mark.asyncio
async def test_only_open_claims_are_read_and_listed_newest_first() -> None:
    claims = [
        _claim(1, 10),
        _claim(2, 11, status="rejected"),
        _claim(3, 12, status="needs_info"),
        _claim(4, 13, status="return_received"),
    ]
    receipts = {10: _receipt(10, 30000), 13: _receipt(13, 30000)}
    summary, _, seen = await _sync(claims, receipts)
    assert [i["claim_id"] for i in catbooks.items_newest_first()] == [4, 1]
    assert "/api/admin/claims/2" not in seen and "/api/admin/claims/3" not in seen
    assert (summary["claims_read"], summary["total"]) == (4, 2)


@pytest.mark.asyncio
async def test_catbooks_down_keeps_previous_items() -> None:
    await _sync([_claim(1, 10)], {10: _receipt(10, 30000)})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = catbooks.CatbooksClient(BASE, httpx.AsyncClient(transport=httpx.MockTransport(down)))
    summary = await catbooks.sync(client)
    assert summary["ok"] is False and "닿지 않음" in summary["error"]
    assert summary["synced_at"]  # 마지막 성공 시각은 남는다
    assert [i["claim_id"] for i in catbooks.items_newest_first()] == [1]


@pytest.mark.asyncio
async def test_sync_does_not_touch_other_finance_records() -> None:
    await _sync([_claim(1, 10)], {10: _receipt(10, 30000)})
    ledger = handlers.state().ledger
    assert ledger.verify_log == [] and ledger.ai_reviews == [] and ledger.refunds == {}


def test_endpoints_sync_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = [_claim(1, 10), _claim(4, 13, status="return_received")]
    receipts = {10: _receipt(10, 30000), 13: _receipt(13, 10000, item_paid=20000)}
    fake, _ = _fake(claims, receipts)
    monkeypatch.setattr(catbooks, "CatbooksClient", lambda: fake)
    client = TestClient(app)
    assert client.get("/api/finance/catbooks/items").json()["data"] == []
    ran = client.get("/api/finance/catbooks/sync", headers={"Origin": "http://localhost:3001"})
    assert ran.headers.get("access-control-allow-origin") == "http://localhost:3001"
    assert ran.json()["data"]["counts"] == {"pass": 1, "blocked": 1, "needs_human": 0}
    body = client.get("/api/finance/catbooks/items").json()
    assert [i["claim_id"] for i in body["data"]] == [4, 1]
    assert body["meta"]["sync"]["ok"] is True and body["meta"]["count"] == 2


def test_cors_stays_get_only() -> None:
    preflight = TestClient(app).options(
        "/api/finance/catbooks/sync",
        headers={"Origin": "http://localhost:3001", "Access-Control-Request-Method": "POST"},
    )
    assert "POST" not in preflight.headers.get("access-control-allow-methods", "")


@pytest.mark.parametrize(("value", "expected"), [(None, 45), ("0", 0), ("10", 10), ("x", 45)])
def test_poll_interval(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int) -> None:
    if value is None:
        monkeypatch.delenv("FINANCE_CATBOOKS_POLL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("FINANCE_CATBOOKS_POLL_SECONDS", value)
    assert catbooks.poll_seconds() == expected


def test_default_url_is_host_docker_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINANCE_CATBOOKS_URL", raising=False)
    assert catbooks.catbooks_url() == "http://host.docker.internal:8030"


@pytest.mark.asyncio
async def test_poll_keeps_going_after_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def flaky_sync() -> dict:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {}

    async def fake_sleep(seconds: float) -> None:
        if len(calls) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(catbooks, "sync", flaky_sync)
    monkeypatch.setattr(catbooks.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await catbooks.poll_forever(30)
    assert len(calls) == 2


def test_no_background_poll_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[int] = []

    async def fake_poll(interval: int) -> None:
        started.append(interval)

    monkeypatch.setattr(catbooks, "poll_forever", fake_poll)
    with TestClient(app):
        pass
    assert started == []
    monkeypatch.setenv("FINANCE_CATBOOKS_POLL_SECONDS", "5")
    with TestClient(app):
        pass
    assert started == [5]
