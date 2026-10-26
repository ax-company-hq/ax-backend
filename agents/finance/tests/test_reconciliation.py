"""② 매출·PG 대사 테스트: 상태 분류와 시드의 데모 불일치."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from finance_agent import books as bk
from finance_agent import reconciliation as rc

TODAY = date(2026, 10, 23)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, fixed_now) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")


def _rec(amount: int, fee: int, settled: bool = True) -> dict:
    return {"amount": amount, "fee": fee, "settled": settled, "paid_out_date": "2026-10-25"}


@pytest.mark.parametrize(
    ("order_amount", "record", "payout", "status", "reason_part"),
    [
        (30000, _rec(30000, 930), TODAY, "matched", None),
        (30000, _rec(30000, 931), TODAY, "matched", None),  # 1원 반올림 오차 허용
        (32000, _rec(30000, 930), TODAY, "mismatch", "2,000원 적음"),
        (30000, _rec(31000, 961), TODAY, "mismatch", "1,000원 많음"),
        (30000, None, TODAY, "missing_in_pg", "PG 정산에 없음"),
        (30000, None, TODAY + timedelta(days=1), "pending", "정산 전"),
        (30000, _rec(30000, 930, settled=False), TODAY, "pending", "지급 예정"),
        (30000, _rec(30000, 1050), TODAY, "fee_anomaly", "120원 많음"),
    ],
)
def test_classify(order_amount, record, payout, status, reason_part) -> None:
    got, reason = rc.classify(order_amount, record, payout, TODAY)
    assert got == status
    if reason_part is None:
        assert reason is None
    else:
        assert reason_part in reason


def test_seed_has_every_status_and_injected_items_are_flagged() -> None:
    books = bk.seed_books()
    result = rc.reconcile(books, None, {})
    assert (result["from"], result["to"]) == ("2026-10-17T00:00:00Z", "2026-10-24T00:00:00Z")
    assert result["counts"]["mismatch"] == 2
    assert result["counts"]["missing_in_pg"] == 1
    assert result["counts"]["fee_anomaly"] == 1
    assert result["counts"]["pending"] > 0 and result["counts"]["matched"] > 0
    by = {i["payment_id"]: i for i in result["items"]}
    expected = {"mismatch": "mismatch", "missing": "missing_in_pg", "fee": "fee_anomaly"}
    for pid, spec in books.pg_overrides.items():
        assert by[pid]["status"] == expected[spec["kind"]]
    missing = next(i for i in result["items"] if i["status"] == "missing_in_pg")
    assert (missing["pg_amount"], missing["fee"], missing["paid_out_date"]) == (None, None, None)
    assert all(i["paid_out_date"] is None for i in result["items"] if i["status"] == "pending")
    assert set(result["items"][0]) == {
        "order_id",
        "payment_id",
        "paid_at",
        "order_amount",
        "pg_amount",
        "fee",
        "expected_fee",
        "pay_out_amount",
        "paid_out_date",
        "status",
        "reason",
        "note",
    }


def test_totals_add_up() -> None:
    result = rc.reconcile(bk.seed_books(), None, {})
    items = result["items"]
    assert result["totals"] == {
        "order_amount": sum(i["order_amount"] for i in items),
        "pg_amount": sum(i["pg_amount"] or 0 for i in items),
        "pg_fee": sum(i["fee"] or 0 for i in items),
        "pay_out": sum(i["pay_out_amount"] or 0 for i in items),
    }
    assert result["totals"]["pay_out"] == result["totals"]["pg_amount"] - result["totals"]["pg_fee"]
    assert sum(result["counts"].values()) == len(items)


def test_single_day_and_notes() -> None:
    books = bk.seed_books()
    pid = next(iter(books.pg_overrides))
    day = next(p["created_at"].date() for p in books.payments() if p["payment_id"] == pid)
    result = rc.reconcile(books, day, {pid: "PG사에 문의함"})
    assert {i["paid_at"][:10] for i in result["items"]} == {day.isoformat()}
    item = next(i for i in result["items"] if i["payment_id"] == pid)
    assert item["note"] == "PG사에 문의함"


def test_exceptions_and_amounts() -> None:
    result = rc.reconcile(bk.seed_books(), None, {})
    exc = rc.exceptions(result)
    assert {i["status"] for i in exc} == {"mismatch", "missing_in_pg", "fee_anomaly"}
    amounts = {i["status"]: rc.exception_amount(i) for i in exc if i["status"] != "mismatch"}
    missing = next(i for i in exc if i["status"] == "missing_in_pg")
    assert amounts["missing_in_pg"] == missing["order_amount"]
    assert sorted(rc.exception_amount(i) for i in exc if i["status"] == "mismatch") == [1000, 2000]
