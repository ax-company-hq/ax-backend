"""장부 자료 모으기(`books.py`)와 공통 도우미(`finance_config.py`) 테스트. 네트워크 없이."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import httpx
import pytest

from finance_agent import books as bk
from finance_agent import finance_config as fc


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")


# ---------------------------------------------------------------- 공통 도우미
@pytest.mark.parametrize(
    ("amount", "rate", "expected"),
    [(15, 0.1, 2), (14, 0.1, 1), (20000, 0.031, 620), (12345, 0.08, 988), (0, 0.1, 0)],
)
def test_rate_amount_rounds_half_up(amount: int, rate: float, expected: int) -> None:
    assert fc.rate_amount(amount, rate) == expected


def test_week_range_defaults_to_last_week() -> None:
    assert fc.week_range(None, date(2026, 10, 23)) == (
        "2026-W42",
        date(2026, 10, 12),
        date(2026, 10, 18),
    )
    assert fc.week_range(None, date(2026, 10, 19))[0] == "2026-W42"  # 월요일도 지난주
    assert fc.week_range("2026-W43", date(2026, 1, 1))[1:] == (
        date(2026, 10, 19),
        date(2026, 10, 25),
    )
    for bad in ("2026-42", "2026-W99", "W42"):
        with pytest.raises(ValueError):
            fc.week_range(bad, date(2026, 10, 23))


def test_month_range_and_parsing() -> None:
    assert fc.month_range(date(2026, 12, 9)) == (date(2026, 12, 1), date(2027, 1, 1))
    assert fc.parse_date("2026-10-23") == date(2026, 10, 23)
    assert fc.parse_date(None) is None and fc.parse_date("") is None
    with pytest.raises(ValueError):
        fc.parse_date("10/23")
    assert fc.parse_dt("2026-10-23T01:02:03Z") == fc.day_start(date(2026, 10, 23)) + timedelta(
        hours=1, minutes=2, seconds=3
    )
    assert fc.parse_dt("어제") is None and fc.parse_dt(None) is None
    assert fc.won(1250000) == "1,250,000원" and fc.won(None) == "확인할 수 없음"
    assert fc.ratio(1, 3) == 0.3333 and fc.ratio(1, 0) is None


def test_config_values() -> None:
    cfg = fc.load_config()
    assert cfg.commission.rate_for("book") == 0.10
    assert cfg.commission.rate_for("stationery") == 0.08
    assert cfg.commission.rate_for(None) == cfg.commission.default_rate == 0.10
    assert (cfg.pg.fee_rate, cfg.pg.payout_days, cfg.settlement.cycle) == (0.031, 2, "weekly")
    assert cfg.settlement.pay_days_after_period_end == 6


# ---------------------------------------------------------------- 목 시드
def test_seed_books_are_relative_to_today_and_deterministic(fixed_now) -> None:
    today = fixed_now.date()
    a, b = bk.seed_books(), bk.seed_books()
    assert [o["total_amount"] for o in a.orders] == [o["total_amount"] for o in b.orders]
    days = sorted({o["created_at"].date() for o in a.orders})
    assert (days[0], days[-1]) == (today - timedelta(days=15), today)
    assert all(o["created_at"] <= fixed_now for o in a.orders)  # 오늘 기록은 00시대뿐
    later = bk.seed_books(today + timedelta(days=30))
    assert [o["total_amount"] for o in later.orders] == [o["total_amount"] for o in a.orders]
    assert later.orders[0]["created_at"] - a.orders[0]["created_at"] == timedelta(days=30)


def test_seed_shapes_follow_shop_api(fixed_now) -> None:
    books = bk.seed_books()
    assert [(s["seller_id"], s["name"]) for s in books.sellers] == [
        ("sel_0001", "책마루"),
        ("sel_0002", "종이나라"),
        ("sel_0003", "북앤펜"),
    ]
    refunded = {}
    for r in books.refunds:
        refunded[r["order_id"]] = refunded.get(r["order_id"], 0) + r["amount"]
    for o in books.orders:
        assert o["total_amount"] == sum(i["paid_amount"] for i in o["items"]) + o["shipping_fee"]
        assert o["payments"][0]["amount"] == o["total_amount"]
        assert o["refunded_amount"] == refunded.get(o["order_id"], 0)
        assert o["order_id"].startswith("ord_") and o["payments"][0]["payment_id"].startswith(
            "pay_"
        )
    assert {c["funded_by"] for c in books.coupons} == {"company", "seller"}
    assert books.source == "seed" and books.mock is True and books.unavailable == []
    assert {e["status"] for e in books.executions or []} == {"executed", "held", "rejected"}
    assert books.meta()["source"] == "seed"


def test_pg_settlements_follow_toss_shape_with_injected_discrepancies(fixed_now) -> None:
    books = bk.seed_books()
    pg = bk.pg_settlements(books)
    rate = fc.load_config().pg.fee_rate
    kinds = {spec["kind"] for spec in books.pg_overrides.values()}
    assert kinds == {"mismatch", "missing", "fee"}
    for pid, spec in books.pg_overrides.items():
        if spec["kind"] == "missing":
            assert pid not in pg
    for p in books.payments():
        rec = pg.get(p["payment_id"])
        if rec is None:
            continue
        assert set(rec) >= {"amount", "fee", "pay_out_amount", "sold_date", "paid_out_date"}
        assert rec["pay_out_amount"] == rec["amount"] - rec["fee"]
        assert rec["paid_out_date"] == (p["created_at"].date() + timedelta(days=2)).isoformat()
        assert rec["settled"] == (p["created_at"].date() + timedelta(days=2) <= fixed_now.date())
        spec = books.pg_overrides.get(p["payment_id"], {})
        if spec.get("kind") == "mismatch":
            assert rec["amount"] == p["amount"] + spec["delta"]
        elif spec.get("kind") == "fee":
            assert rec["fee"] == fc.rate_amount(rec["amount"], spec["fee_rate"])
        else:
            assert (rec["amount"], rec["fee"]) == (p["amount"], fc.rate_amount(p["amount"], rate))


def test_live_overrides_are_deterministic() -> None:
    ids = [f"pay_{n:04d}" for n in range(20, 0, -1)]
    plan = bk._live_overrides(ids)
    assert plan == bk._live_overrides(list(reversed(ids)))
    assert {v["kind"] for v in plan.values()} == {"mismatch", "missing", "fee"}
    assert bk._live_overrides(["pay_1", "pay_2"]) == {}


# ---------------------------------------------------------------- 실제 모드 (shop-api, safety)
SHOP_ORDER = {
    "order_id": "ord_0001",
    "buyer_id": "buy_0001",
    "status": "paid",
    "total_amount": 30000,
    "refunded_amount": 12000,
    "shipping_fee": 0,
    "points_used": 0,
    "created_at": "2026-10-20T01:00:00Z",
    "delivered_at": None,
    "items": [
        {
            "order_item_id": "oit_0001",
            "product_id": "prd_0001",
            "seller_id": "sel_0001",
            "title": "책",
            "qty": 1,
            "unit_price": 18000,
            "points_share": 0,
            "paid_amount": 18000,
        },
        {
            "order_item_id": "oit_0002",
            "product_id": "prd_0004",
            "seller_id": "sel_0002",
            "title": "노트",
            "qty": 1,
            "unit_price": 12000,
            "points_share": 0,
            "paid_amount": 12000,
        },
    ],
    "payments": [
        {
            "payment_id": "pay_0001",
            "amount": 30000,
            "method_code": "card",
            "status": "succeeded",
            "failure_code": None,
            "created_at": "2026-10-20T01:00:05Z",
        }
    ],
}
EXECUTION = {
    "execution_id": "exe_1",
    "created_at": "2026-10-21T02:00:00Z",
    "request": {
        "action": "refund_request",
        "requester": "support",
        "context_id": "ctx_req_1",
        "params": {"order_id": "ord_0001", "refund_amount": 12000},
    },
    "decision": {"execution_id": "exe_1", "grade": "A", "status": "executed"},
}


def _transport(seen: list[httpx.Request], down: set[str] = frozenset()):  # type: ignore[assignment]
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET", "재무는 원천에 GET만 보낸다"
        if request.url.host in down:
            raise httpx.ConnectError("down")
        data = {
            "/api/shop/orders": [SHOP_ORDER, {"order_id": "broken"}],
            "/api/shop/products": [
                {"product_id": "prd_0001", "category_code": "book"},
                {"product_id": "prd_0004", "category_code": "stationery"},
            ],
            "/api/shop/sellers": [{"seller_id": "sel_0001", "name": "책마루"}],
            "/api/shop/coupons": [
                {
                    "coupon_id": "cpn_1",
                    "buyer_id": "buy_0001",
                    "total_value": 5000,
                    "count": 1,
                    "unit_value": 5000,
                    "created_at": "2026-10-21T00:00:00Z",
                }
            ],
            "/api/safety/thresholds": {"version": 1, "rules": []},
            "/api/safety/executions": [EXECUTION],
        }.get(request.url.path)
        if data is None:
            return httpx.Response(404)
        return httpx.Response(200, json={"data": data, "meta": {}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_live_books_read_shop_api_and_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AX_SHOP_API_URL", "http://shop")
    monkeypatch.setenv("AX_SAFETY_URL", "http://safety")
    seen: list[httpx.Request] = []
    books = asyncio.run(bk.live_books(_transport(seen)))
    assert (books.source, books.mock) == ("shop-api", False)
    assert [o["order_id"] for o in books.orders] == ["ord_0001"]
    assert "orders_partial" in books.unavailable and "claims" in books.unavailable
    assert books.claims is None  # 클레임 목록 조회가 없어 확인할 수 없음
    assert [i["category_code"] for i in books.orders[0]["items"]] == ["book", "stationery"]
    coupon_call = next(r for r in seen if r.url.path == "/api/shop/coupons")
    assert coupon_call.headers["authorization"] == "Bearer mock:finance"
    assert books.coupons[0]["funded_by"] == "company"
    assert books.executions and books.executions[0]["action"] == "refund_request"
    assert books.notes == {"refund_basis": "order_created_at"}
    # 환불은 품목 결제 비율로 판매자에게 나눈다 (18,000 : 12,000)
    assert sorted((r["seller_id"], r["amount"]) for r in books.refunds) == [
        ("sel_0001", 7200),
        ("sel_0002", 4800),
    ]


def test_live_books_mark_missing_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AX_SHOP_API_URL", "http://shop")
    monkeypatch.setenv("AX_SAFETY_URL", "http://safety")
    books = asyncio.run(bk.live_books(_transport([], {"safety"})))
    assert {"thresholds", "executions", "claims"} <= set(books.unavailable)
    assert books.thresholds is None and books.executions is None
    books = asyncio.run(bk.live_books(_transport([], {"shop"})))
    assert {"orders", "products", "sellers", "coupons"} <= set(books.unavailable)
    assert books.orders == [] and books.meta()["unavailable"] == books.unavailable


def test_normalizers_refuse_bad_shapes() -> None:
    assert bk.normalize_order({"order_id": "ord_1"}, {}) is None
    assert bk.normalize_order(SHOP_ORDER | {"items": [{"qty": "1"}]}, {}) is None
    assert bk.normalize_execution({"execution_id": "x"}) is None
    norm = bk.normalize_execution(EXECUTION)
    assert norm is not None and (norm["status"], norm["params"]["refund_amount"]) == (
        "executed",
        12000,
    )


def test_load_books_uses_seed_in_mock_mode(fixed_now) -> None:
    books = asyncio.run(bk.load_books())
    assert books.source == "seed" and len(books.orders) > 100
