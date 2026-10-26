"""③ 판매자 정산: 판매자별 정산 내역서와 보류 사유.

정산금 `payout` = 판매액 `gross` − 판매 수수료 `commission` − 판매자 부담 쿠폰 `seller_coupon`
− 환불 `refunds`. 기간은 주 단위(월~일), 기본은 지난주.

- 판매액·수수료: 기간 안에 결제된 주문의 그 판매자 품목 (수수료는 카테고리 수수료율, 품목별 반올림)
- 판매자 부담 쿠폰: 기간 안에 발급된 `funded_by="seller"` 쿠폰
- 환불: 기간 안 환불 중 그 판매자 몫

보류 사유 (하나라도 있으면 `held`, 정산 묶음에서 빠진다):
- `open_claim`: 처리 중인 환불·교환 (클레임 자료를 못 받으면 `claims_unknown`, fail-closed)
- `unconfirmed_purchase`: 기간 안 주문 중 아직 배송 완료 전 (구매 미확정)
- `risky_seller`: 정산 보류(`settlement_hold`)가 걸린 위험 판매자
- `negative_payout`: 정산금이 0원 이하

지급 확정은 재무가 하지 않는다. 묶음을 `execute("confirm_settlement")`로 요청하면 안전 관문이
항상 C로 보류하고 사람이 승인한다 (`settlement_batch.py`의 API 쪽).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from finance_agent import finance_config as fc
from finance_agent.books import Books, in_range, item_gross, order_sold


def _paid_at(order: dict[str, Any]):
    times = [p["created_at"] for p in order["payments"] if p.get("status") == "succeeded"]
    return min(times) if times else None


def statements(
    books: Books,
    period: str | None,
    held_sellers: set[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    cfg = fc.load_config()
    today = today or fc.today()
    name, monday, sunday = fc.week_range(period, today)
    start, end = fc.day_start(monday), fc.day_start(sunday + timedelta(days=1))
    risky = set(books.risky_sellers) | (held_sellers or set())

    sellers = [s["seller_id"] for s in books.sellers if s.get("seller_id")]
    for o in books.orders:  # 판매자 목록에 없는 판매자의 품목도 빠뜨리지 않는다
        for i in o["items"]:
            if i.get("seller_id") and i["seller_id"] not in sellers:
                sellers.append(i["seller_id"])

    rows = []
    for seller_id in sellers:
        gross = commission = 0
        order_ids: set[str] = set()
        rates: set[float] = set()
        unconfirmed: set[str] = set()
        for o in books.orders:
            if not order_sold(o) or not in_range(_paid_at(o), start, end):
                continue
            mine = [i for i in o["items"] if i.get("seller_id") == seller_id]
            if not mine:
                continue
            order_ids.add(o["order_id"])
            if o.get("delivered_at") is None:
                unconfirmed.add(o["order_id"])
            for i in mine:
                g = item_gross(i)
                rate = cfg.commission.rate_for(i.get("category_code"))
                rates.add(rate)
                gross += g
                commission += fc.rate_amount(g, rate)
        seller_coupon = sum(
            c["total_value"]
            for c in books.coupons
            if c.get("funded_by") == "seller"
            and c.get("seller_id") == seller_id
            and in_range(c.get("created_at"), start, end)
        )
        refunds = sum(
            r["amount"]
            for r in books.refunds
            if r.get("seller_id") == seller_id and in_range(r.get("created_at"), start, end)
        )
        payout = gross - commission - seller_coupon - refunds
        if len(rates) == 1:
            rate_shown = rates.pop()
        elif gross:
            rate_shown = round(commission / gross, 4)
        else:
            rate_shown = cfg.commission.default_rate

        reasons = []
        if books.claims is None:
            reasons.append({"code": "claims_unknown", "label": "처리 중 클레임을 확인할 수 없음"})
        else:
            open_claims = [
                c
                for c in books.claims
                if c.get("seller_id") == seller_id and c.get("status") == "open"
            ]
            if open_claims:
                reasons.append(
                    {"code": "open_claim", "label": f"처리 중인 환불·교환 {len(open_claims)}건"}
                )
        if unconfirmed:
            reasons.append(
                {"code": "unconfirmed_purchase", "label": f"구매 미확정 주문 {len(unconfirmed)}건"}
            )
        if seller_id in risky:
            reasons.append({"code": "risky_seller", "label": "위험 판매자 정산 보류 중"})
        if order_ids and payout <= 0:
            reasons.append({"code": "negative_payout", "label": "정산금이 0원 이하"})

        if not order_ids and not seller_coupon and not refunds:
            continue  # 이번 기간 거래가 없는 판매자는 내역서를 만들지 않는다
        rows.append(
            {
                "seller_id": seller_id,
                "name": books.seller_name(seller_id),
                "orders": len(order_ids),
                "gross": gross,
                "commission_rate": rate_shown,
                "commission": commission,
                "seller_coupon": seller_coupon,
                "refunds": refunds,
                "payout": payout,
                "hold_reasons": reasons,
                "status": "held" if reasons else "ready",
                "review_id": None,
            }
        )
    pay_date = sunday + timedelta(days=cfg.settlement.pay_days_after_period_end)
    return {
        "period": name,
        "from": fc.iso(start),
        "to": fc.iso(end),
        "pay_date": pay_date.isoformat(),
        "statements": rows,
    }


def apply_batch(result: dict[str, Any], batch: dict[str, Any] | None) -> dict[str, Any]:
    """묶음 요청 상태를 내역서에 반영하고 `batch`를 붙인다."""
    empty = {
        "status": "none",
        "review_id": None,
        "execution_id": None,
        "total_payout": 0,
        "seller_ids": [],
    }
    shown = {k: batch.get(k, v) for k, v in empty.items()} if batch else empty
    if batch and shown["status"] != "none":
        for row in result["statements"]:
            if row["seller_id"] in shown["seller_ids"]:
                row["status"] = shown["status"]
                row["review_id"] = shown["review_id"]
    return result | {"batch": shown}


def ready(result: dict[str, Any]) -> list[dict[str, Any]]:
    """묶음에 넣을 판매자: 보류 사유가 없고 아직 요청·승인되지 않음 (거절된 묶음은 다시 넣는다)."""
    return [
        r
        for r in result["statements"]
        if not r["hold_reasons"] and r["status"] in ("ready", "rejected")
    ]
