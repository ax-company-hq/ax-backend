"""① 결산·리포트 계산 테스트."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from finance_agent import closing
from finance_agent import finance_config as fc


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, fixed_now) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")


def _day_books(kit):
    when = kit.at("2026-10-20", "09:00")
    orders = [
        kit.order("ord_1", when, [("sel_a", "book", 2, 10000)]),  # 20,000 → 수수료 2,000
        kit.order("ord_2", when, [("sel_b", "stationery", 1, 12345)], shipping=3000),  # 988
        kit.order("ord_3", kit.at("2026-10-21"), [("sel_a", "book", 1, 50000)]),  # 다른 날
        kit.order("ord_4", when, [("sel_a", "book", 1, 9000)], payment_status="failed"),
    ]
    coupons = [
        {"coupon_id": "cpn_1", "total_value": 3000, "funded_by": "company", "created_at": when},
        {
            "coupon_id": "cpn_2",
            "total_value": 2000,
            "funded_by": "seller",
            "seller_id": "sel_a",
            "created_at": when,
        },
        {
            "coupon_id": "cpn_3",
            "total_value": 9999,
            "funded_by": "company",
            "created_at": kit.at("2026-10-19"),
        },
    ]
    refunds = [
        {
            "refund_id": "rfd_1",
            "order_id": "ord_1",
            "seller_id": "sel_a",
            "amount": 10000,
            "created_at": kit.at("2026-10-20", "15:00"),
        }
    ]
    return kit.books(orders=orders, coupons=coupons, refunds=refunds)


def test_totals_follow_the_formulas(kit) -> None:
    books = _day_books(kit)
    t = closing.compute_totals(
        books, fc.day_start(date(2026, 10, 20)), fc.day_start(date(2026, 10, 21))
    )
    assert t.order_count == 2  # 결제 실패 주문·다른 날 주문은 빠진다
    assert t.gmv == 20000 + 12345  # 품목 판매가 합계 (배송비 제외)
    assert t.commission_revenue == 2000 + 988  # 도서 10%, 문구 8% (반올림)
    assert t.coupon_cost == 3000  # 판매자 부담 쿠폰·다른 날 쿠폰 제외
    assert t.refund_total == 10000
    # PG 수수료는 결제 금액(배송비 포함) × 3.1%. 지급 예정일이 지난 결제라 PG 기록 값
    assert t.pg_fee == fc.rate_amount(20000, 0.031) + fc.rate_amount(15345, 0.031)
    assert t.net_result == t.commission_revenue - t.coupon_cost - t.pg_fee


def test_lines_and_balanced_journal(kit) -> None:
    books = _day_books(kit)
    t = closing.compute_totals(
        books, fc.day_start(date(2026, 10, 20)), fc.day_start(date(2026, 10, 21))
    )
    lines = closing.lines(t)
    assert [ln["code"] for ln in lines] == [
        "gmv",
        "commission_revenue",
        "coupon_cost",
        "refund_total",
        "pg_fee",
        "net_result",
    ]
    by = {ln["code"]: ln["amount"] for ln in lines}
    assert (by["coupon_cost"], by["refund_total"], by["pg_fee"]) == (-3000, -10000, -t.pg_fee)
    assert by["net_result"] == by["commission_revenue"] + by["coupon_cost"] + by["pg_fee"]
    journal = closing.journal(t)
    assert sum(j["debit"] for j in journal) == sum(j["credit"] for j in journal)
    assert journal[0] == {
        "account": "외상매출금(PG)",
        "debit": t.gmv,
        "credit": 0,
        "memo": "카드 결제 매출",
    }
    assert all(j["debit"] >= 0 and j["credit"] >= 0 for j in journal)


def test_report_shape_and_template_commentary(kit) -> None:
    books = _day_books(kit)
    report = asyncio.run(closing.closing_report(books, "day", date(2026, 10, 20), None))
    assert set(report) == {
        "period",
        "date",
        "from",
        "to",
        "lines",
        "journal",
        "commentary",
        "commentary_source",
        "status",
        "confirmed_by",
        "confirmed_at",
    }
    assert (report["from"], report["to"]) == ("2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z")
    assert (report["status"], report["confirmed_by"], report["commentary_source"]) == (
        "draft",
        None,
        "template",
    )
    assert "32,345원" in report["commentary"] and "주문 2건" in report["commentary"]
    confirmed = asyncio.run(
        closing.closing_report(books, "day", date(2026, 10, 20), {"by": "태윤", "at": "x"})
    )
    assert (confirmed["status"], confirmed["confirmed_by"]) == ("confirmed", "태윤")


def test_month_period(kit) -> None:
    books = _day_books(kit)
    report = asyncio.run(closing.closing_report(books, "month", date(2026, 10, 20), None))
    assert (report["date"], report["from"], report["to"]) == (
        "2026-10-01",
        "2026-10-01T00:00:00Z",
        "2026-11-01T00:00:00Z",
    )
    gmv = next(ln["amount"] for ln in report["lines"] if ln["code"] == "gmv")
    assert gmv == 20000 + 12345 + 50000
    assert "2026년 10월" in report["commentary"]


def test_empty_day_says_no_orders(kit) -> None:
    report = asyncio.run(closing.closing_report(kit.books(), "day", date(2026, 10, 23), None))
    assert "아직 결제된 주문이 없습니다" in report["commentary"]
    assert all(ln["amount"] == 0 for ln in report["lines"]) and report["journal"] == []


def test_week_period(kit) -> None:
    books = _day_books(kit)
    report = asyncio.run(closing.closing_report(books, "week", date(2026, 10, 22), None))
    assert (report["date"], report["from"], report["to"]) == (
        "2026-10-19",
        "2026-10-19T00:00:00Z",
        "2026-10-26T00:00:00Z",
    )
    gmv = next(ln["amount"] for ln in report["lines"] if ln["code"] == "gmv")
    assert gmv == 20000 + 12345 + 50000
    assert "10월 19일부터 한 주" in report["commentary"]


def test_missing_orders_make_lines_unknown_not_zero(kit, monkeypatch: pytest.MonkeyPatch) -> None:
    books = _day_books(kit)
    books.orders, books.refunds, books.unavailable = [], [], ["orders"]
    books.mock = False
    fake = _FakeLLM("m")
    monkeypatch.setattr(closing, "LLMClient", lambda dept: fake)
    report = asyncio.run(closing.closing_report(books, "day", date(2026, 10, 20), None))
    amounts = {ln["code"]: ln["amount"] for ln in report["lines"]}
    assert amounts["gmv"] is None and amounts["net_result"] is None
    assert amounts["coupon_cost"] == -3000  # 쿠폰 자료는 받았으니 숫자가 있다
    assert [j["account"] for j in report["journal"]] == ["판매촉진비(쿠폰)", "미지급금(쿠폰)"]
    assert report["commentary_source"] == "template" and fake.prompts == []
    assert "주문 자료를 받지 못해" in report["commentary"]
    assert "총거래액은" not in report["commentary"]  # 0원 매출 문장을 쓰지 않는다


def test_missing_coupons_make_net_result_unknown(kit) -> None:
    books = _day_books(kit)
    books.coupons, books.unavailable = [], ["coupons"]
    report = asyncio.run(closing.closing_report(books, "day", date(2026, 10, 20), None))
    amounts = {ln["code"]: ln["amount"] for ln in report["lines"]}
    assert amounts["gmv"] == 32345
    assert amounts["coupon_cost"] is None and amounts["net_result"] is None
    assert "쿠폰 자료를 받지 못해" in report["commentary"]


def test_partial_orders_keep_numbers(kit) -> None:
    books = _day_books(kit)
    books.unavailable = ["orders_partial"]
    assert closing.unknown_codes(books) == frozenset()


class _FakeLLM:
    def __init__(self, model: str | None, fail: bool = False) -> None:
        self.settings = type("S", (), {"model": model})()
        self.fail = fail
        self.prompts: list[str] = []

    async def structured(self, prompt, schema, untrusted=None):
        self.prompts.append(prompt)
        if self.fail:
            raise NotImplementedError
        return schema(text="LLM이 쓴 설명")


@pytest.mark.parametrize(
    ("mock", "model", "fail", "source"),
    [
        (True, "m", False, "template"),
        (False, None, False, "template"),
        (False, "m", True, "template"),
        (False, "m", False, "llm"),
    ],
)
def test_commentary_uses_llm_only_when_live_and_configured(
    kit, monkeypatch: pytest.MonkeyPatch, mock: bool, model: str | None, fail: bool, source: str
) -> None:
    fake = _FakeLLM(model, fail)
    monkeypatch.setattr(closing, "LLMClient", lambda dept: fake)
    t = closing.Totals(1000, 100, 10, 0, 31, 1)
    text, got = asyncio.run(closing.write_commentary("오늘", t, mock))
    assert got == source
    if source == "llm":
        assert text == "LLM이 쓴 설명" and "59" in fake.prompts[0]  # 코드가 계산한 영업 결과
    else:
        assert text == closing.template_commentary("오늘", t)
