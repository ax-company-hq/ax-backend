"""⑦ 내부통제·사후 감사 테스트: 의심 코드마다 잡히는지, 안 잡혀야 할 때 안 잡히는지."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from finance_agent import audit
from finance_agent import books as bk
from finance_agent import finance_config as fc

ALL_CODES = {
    "split_under_limit",
    "repeat_refund",
    "duplicate_coupon",
    "refund_rate_jump",
    "amount_recheck_failed",
    "gate_hold_trend",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")


def _report(books, days: int = 7):
    return asyncio.run(audit.audit_report(books, days))


def _by_code(findings):
    out: dict[str, list] = {}
    for f in findings:
        out.setdefault(f["code"], []).append(f)
    return out


def test_seed_triggers_every_finding_code(fixed_now) -> None:
    data, missing = _report(bk.seed_books())
    assert missing == []
    assert (data["from"], data["to"]) == ("2026-10-17T00:00:00Z", "2026-10-24T00:00:00Z")
    found = _by_code(data["findings"])
    assert set(found) == ALL_CODES
    for f in data["findings"]:
        assert set(f) == {"code", "severity", "title", "detail", "refs"}
        assert f["severity"] in ("high", "medium", "low")
    order = [audit.SEVERITY_ORDER[f["severity"]] for f in data["findings"]]
    assert order == sorted(order)  # 심각한 것 먼저

    [split] = found["split_under_limit"]
    assert "buy_0007" in split["detail"] and "19,900원" in split["detail"]
    assert "20,000원" in split["detail"] and split["severity"] == "high"
    assert len([r for r in split["refs"] if r.startswith("cpn_")]) == 3
    assert any(r.startswith("exe_") for r in split["refs"])
    [repeat] = found["repeat_refund"]
    assert "buy_0013" in repeat["detail"] and "3건" in repeat["detail"]
    [dup] = found["duplicate_coupon"]
    assert "buy_0011" in dup["detail"] and "ctx_inc_0104" in dup["detail"]
    [jump] = found["refund_rate_jump"]
    assert "환불률" in jump["detail"]
    [recheck] = found["amount_recheck_failed"]
    assert "결제 금액을 넘습니다" in recheck["detail"] and recheck["refs"][0].startswith("exe_")
    [trend] = found["gate_hold_trend"]
    assert "보류 3건·거절 1건" in trend["detail"] and "1건)" in trend["detail"]


def test_seed_stats(fixed_now) -> None:
    data, _ = _report(bk.seed_books())
    stats = data["stats"]
    assert set(stats) == {
        "refund_count",
        "refund_amount",
        "refund_rate",
        "refund_rate_prev",
        "coupon_count",
        "coupon_amount",
        "gate_held",
        "gate_rejected",
    }
    assert (stats["refund_count"], stats["gate_held"], stats["gate_rejected"]) == (6, 3, 1)
    assert stats["refund_rate"] > stats["refund_rate_prev"] > 0
    assert stats["coupon_amount"] > 0 and stats["coupon_count"] > 0


@pytest.mark.parametrize("offset", range(7))
def test_every_code_on_any_weekday(monkeypatch: pytest.MonkeyPatch, offset: int) -> None:
    now = datetime(2026, 10, 19, 12, tzinfo=UTC) + timedelta(days=offset)
    monkeypatch.setattr(fc, "now", lambda: now)
    data, _ = _report(bk.seed_books())
    assert {f["code"] for f in data["findings"]} == ALL_CODES


# ---------------------------------------------------------------- 규칙별
def _coupon(kit, cid, buyer, unit, when, ctx=None, count=1, purpose="compensation"):
    return {
        "coupon_id": cid,
        "buyer_id": buyer,
        "unit_value": unit,
        "count": count,
        "total_value": unit * count,
        "purpose_code": purpose,
        "context_id": ctx,
        "created_at": when,
        "funded_by": "company",
    }


def test_split_coupons_needs_near_limit_and_short_window(kit, fixed_now) -> None:
    t = kit.at("2026-10-22", "10:00")
    near = [
        _coupon(kit, f"cpn_{i}", "buy_x", 18000, t + timedelta(minutes=20 * i)) for i in range(3)
    ]
    found = _by_code(_report(kit.books(coupons=near))[0]["findings"])
    assert "split_under_limit" in found
    spread = [c | {"created_at": t + timedelta(hours=2 * i)} for i, c in enumerate(near)]
    assert "split_under_limit" not in _by_code(_report(kit.books(coupons=spread))[0]["findings"])
    small = [c | {"unit_value": 10000, "total_value": 10000} for c in near]
    assert "split_under_limit" not in _by_code(_report(kit.books(coupons=small))[0]["findings"])
    one = [_coupon(kit, "cpn_9", "buy_x", 19000, t, count=3)]  # 한 번에 3장은 쪼개기가 아니다
    assert "split_under_limit" not in _by_code(_report(kit.books(coupons=one))[0]["findings"])


def test_split_refunds_under_refund_limit(kit, fixed_now) -> None:
    t = kit.at("2026-10-22", "09:00")
    refunds = [
        {
            "refund_id": f"rfd_{i}",
            "order_id": f"ord_{i}",
            "buyer_id": "buy_y",
            "seller_id": "sel_a",
            "amount": 95000,
            "created_at": t + timedelta(hours=i),
        }
        for i in range(2)
    ]
    [f] = _by_code(_report(kit.books(refunds=refunds))[0]["findings"])["split_under_limit"]
    assert "buy_y" in f["detail"] and f["refs"] == ["rfd_0", "rfd_1"]
    over = [
        r | {"amount": 100000} for r in refunds
    ]  # 한도 이상은 관문이 C로 막는 건이라 쪼개기 아님
    assert "split_under_limit" not in _by_code(_report(kit.books(refunds=over))[0]["findings"])


def test_repeat_refund_counts_orders(kit, fixed_now) -> None:
    t = kit.at("2026-10-21")
    refunds = [
        {
            "refund_id": f"rfd_{i}",
            "order_id": "ord_same" if i < 2 else f"ord_{i}",
            "buyer_id": "buy_z",
            "seller_id": "sel_a",
            "amount": 1000,
            "created_at": t,
        }
        for i in range(3)
    ]
    assert "repeat_refund" not in _by_code(_report(kit.books(refunds=refunds))[0]["findings"])
    refunds[1]["order_id"] = "ord_other"
    assert "repeat_refund" in _by_code(_report(kit.books(refunds=refunds))[0]["findings"])


def test_duplicate_coupon_same_context_only(kit, fixed_now) -> None:
    t = kit.at("2026-10-21")
    two = [
        _coupon(kit, "cpn_1", "buy_q", 5000, t, "ctx_inc_1"),
        _coupon(kit, "cpn_2", "buy_q", 5000, t, "ctx_inc_1"),
    ]
    assert "duplicate_coupon" in _by_code(_report(kit.books(coupons=two))[0]["findings"])
    two[1]["context_id"] = "ctx_inc_2"
    assert "duplicate_coupon" not in _by_code(_report(kit.books(coupons=two))[0]["findings"])
    camp = [c | {"purpose_code": "campaign", "context_id": "ctx_cmp_1"} for c in two]
    assert "duplicate_coupon" not in _by_code(_report(kit.books(coupons=camp))[0]["findings"])


@pytest.mark.parametrize(
    ("rate", "prev", "jumped", "severity"),
    [
        (0.05, 0.03, True, "medium"),
        (0.07, 0.03, True, "high"),
        (0.035, 0.03, False, None),
        (0.035, 0.02, True, "medium"),
        (0.015, 0.0, True, "high"),
        (0.005, 0.0, False, None),
        (None, 0.03, False, None),
    ],
)
def test_refund_rate_jump_rule(rate, prev, jumped, severity) -> None:
    out = audit.refund_rate_jump(rate, prev, 7)
    assert bool(out) == jumped
    if jumped:
        assert out[0]["severity"] == severity


def test_amount_recheck_flags_exceeding_refund(kit, fixed_now) -> None:
    when = kit.at("2026-10-20")
    orders = [kit.order("ord_1", when, [("sel_a", "book", 1, 20000)])]
    executions = [
        {
            "execution_id": "exe_1",
            "created_at": when + timedelta(hours=1),
            "action": "refund_request",
            "requester": "support",
            "context_id": "ctx_req_1",
            "params": {"order_id": "ord_1", "refund_amount": 15000},
            "status": "executed",
            "computed_amount": 15000,
        },
        {
            "execution_id": "exe_2",
            "created_at": when + timedelta(hours=2),
            "action": "refund_request",
            "requester": "support",
            "context_id": "ctx_req_2",
            "params": {"order_id": "ord_1", "refund_amount": 6000},
            "status": "executed",
            "computed_amount": 6000,
        },
        {
            "execution_id": "exe_3",
            "created_at": when,
            "action": "issue_coupon",
            "requester": "marketing",
            "context_id": "ctx_inc_9",
            "params": {
                "count": 2,
                "unit_value": 5000,
                "total_value": 10000,
                "purpose_code": "compensation",
                "buyer_id": "buy_1",
            },
            "status": "executed",
            "computed_amount": 10000,
        },
        {
            "execution_id": "exe_4",
            "created_at": when,
            "action": "refund_request",
            "requester": "support",
            "context_id": "ctx_req_4",
            "params": {"order_id": "ord_1", "refund_amount": 99999},
            "status": "held",
            "computed_amount": 99999,
        },
    ]
    found = _by_code(_report(kit.books(orders=orders, executions=executions))[0]["findings"])
    [f] = found["amount_recheck_failed"]
    assert f["refs"] == ["exe_2", "ord_1"]  # 첫 환불은 맞고, 두 번째가 결제 금액을 넘음
    assert "이미 15,000원" in f["detail"]
    # 장애 보상 쿠폰은 사후에 피해 고객 자료가 없어도 금액만 맞으면 의심하지 않는다


def test_gate_trend_and_missing_executions(kit, fixed_now) -> None:
    t = kit.at("2026-10-22")
    held = [
        {
            "execution_id": f"exe_{i}",
            "created_at": t,
            "action": "issue_coupon",
            "status": "held",
            "params": {},
        }
        for i in range(3)
    ]
    data, _ = _report(kit.books(executions=held))
    assert "gate_hold_trend" in _by_code(data["findings"])
    prev = [
        e | {"execution_id": f"exe_p{i}", "created_at": t - timedelta(days=8)}
        for i, e in enumerate(held)
    ]
    data, _ = _report(kit.books(executions=held + prev))
    assert "gate_hold_trend" not in _by_code(data["findings"])  # 지난 기간과 같으면 추이 아님
    data, missing = _report(kit.books(executions=None))
    assert (data["stats"]["gate_held"], data["stats"]["gate_rejected"]) == (None, None)
    assert missing == ["stats.gate_held", "stats.gate_rejected"]


def test_quiet_books_have_no_findings(kit, fixed_now) -> None:
    data, _ = _report(kit.books())
    assert data["findings"] == []
    assert data["stats"]["refund_rate"] is None  # 거래가 없으면 비율은 확인할 수 없음
