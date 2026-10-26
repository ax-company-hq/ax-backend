"""③ 판매자 정산 테스트: 정산 공식과 보류 사유."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from finance_agent import books as bk
from finance_agent import finance_config as fc
from finance_agent import settlement as st


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")


def test_seed_last_week_statements(fixed_now) -> None:
    result = st.statements(bk.seed_books(), None)
    assert (result["period"], result["pay_date"]) == ("2026-W42", "2026-10-24")
    assert (result["from"], result["to"]) == ("2026-10-12T00:00:00Z", "2026-10-19T00:00:00Z")
    rows = {r["seller_id"]: r for r in result["statements"]}
    assert set(rows) == {"sel_0001", "sel_0002", "sel_0003"}
    for r in rows.values():
        assert r["payout"] == r["gross"] - r["commission"] - r["seller_coupon"] - r["refunds"]
        assert set(r) == {
            "seller_id",
            "name",
            "orders",
            "gross",
            "commission_rate",
            "commission",
            "seller_coupon",
            "refunds",
            "payout",
            "hold_reasons",
            "status",
            "review_id",
        }
    assert rows["sel_0002"]["status"] == "held"
    assert rows["sel_0002"]["hold_reasons"] == [
        {"code": "open_claim", "label": "처리 중인 환불·교환 1건"}
    ]
    assert (rows["sel_0001"]["status"], rows["sel_0003"]["status"]) == ("ready", "ready")
    assert (
        rows["sel_0001"]["commission_rate"] == 0.10 and rows["sel_0002"]["commission_rate"] == 0.08
    )
    assert 0.08 < rows["sel_0003"]["commission_rate"] < 0.10  # 도서·문구 섞임 → 실효 수수료율
    assert rows["sel_0003"]["seller_coupon"] == 7 * 2000  # 매일 판매자 부담 2,000원


@pytest.mark.parametrize("offset", range(7))
def test_two_sellers_ready_on_any_weekday(monkeypatch: pytest.MonkeyPatch, offset: int) -> None:
    now = datetime(2026, 10, 19, 12, tzinfo=UTC) + timedelta(days=offset)
    monkeypatch.setattr(fc, "now", lambda: now)
    rows = {r["seller_id"]: r["status"] for r in st.statements(bk.seed_books(), None)["statements"]}
    assert rows == {"sel_0001": "ready", "sel_0002": "held", "sel_0003": "ready"}


def test_hold_reasons(kit, fixed_now) -> None:
    when = kit.at("2026-10-14")
    orders = [
        kit.order("ord_1", when, [("sel_a", "book", 1, 20000)], delivered=False),
        kit.order("ord_2", when, [("sel_b", "book", 1, 10000)]),
    ]
    refunds = [
        {
            "refund_id": "rfd_1",
            "order_id": "ord_2",
            "seller_id": "sel_b",
            "amount": 9500,
            "created_at": when,
        }
    ]
    books = kit.books(orders=orders, refunds=refunds)
    rows = {r["seller_id"]: r for r in st.statements(books, None, {"sel_a"})["statements"]}
    assert [h["code"] for h in rows["sel_a"]["hold_reasons"]] == [
        "unconfirmed_purchase",
        "risky_seller",
    ]
    assert rows["sel_b"]["payout"] == 10000 - 1000 - 9500
    assert [h["code"] for h in rows["sel_b"]["hold_reasons"]] == ["negative_payout"]
    unknown = kit.books(orders=orders, claims=None)
    rows = {r["seller_id"]: r for r in st.statements(unknown, None)["statements"]}
    assert rows["sel_b"]["hold_reasons"][0]["code"] == "claims_unknown"  # fail-closed
    assert all(r["status"] == "held" for r in rows.values())


def test_period_parameter_and_quiet_sellers(kit, fixed_now) -> None:
    books = kit.books(
        orders=[kit.order("ord_1", kit.at("2026-10-20"), [("sel_a", "book", 1, 1000)])]
    )
    assert st.statements(books, None)["statements"] == []  # 지난주 거래 없음
    week = st.statements(books, "2026-W43")
    assert [r["seller_id"] for r in week["statements"]] == ["sel_a"]
    with pytest.raises(ValueError):
        st.statements(books, "2026-43")


def test_apply_batch(fixed_now) -> None:
    result = st.statements(bk.seed_books(), None)
    plain = st.apply_batch(dict(result), None)
    assert plain["batch"] == {
        "status": "none",
        "review_id": None,
        "execution_id": None,
        "total_payout": 0,
        "seller_ids": [],
    }
    batch = {
        "status": "requested",
        "review_id": "rev_1",
        "execution_id": "exe_1",
        "total_payout": 10,
        "seller_ids": ["sel_0001"],
        "attempt": 1,
    }
    shown = st.apply_batch(st.statements(bk.seed_books(), None), batch)
    rows = {r["seller_id"]: r for r in shown["statements"]}
    assert (rows["sel_0001"]["status"], rows["sel_0001"]["review_id"]) == ("requested", "rev_1")
    assert rows["sel_0003"]["status"] == "ready"
    assert "attempt" not in shown["batch"] and shown["batch"]["review_id"] == "rev_1"
    assert [r["seller_id"] for r in st.ready(shown)] == ["sel_0003"]
    assert date.fromisoformat(shown["pay_date"]).weekday() == 5
