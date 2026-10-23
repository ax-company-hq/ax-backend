"""⑦ 내부통제·이상 탐지: 사후 감사 리포트.

건별 검사는 안전 관문 코드 규칙이 한다. 재무는 지나간 기록을 전수로 다시 본다.
기간은 오늘까지 `days`일, 비교 기간은 그 바로 앞 같은 길이. 모든 판정은 코드 규칙이다
(기준값은 `finance_policy.toml [audit]`, 한도는 기준표).

- `split_under_limit`: 같은 고객에게 한도 바로 아래(한도의 `near_limit_ratio` 이상) 쿠폰을
  `split_window_minutes` 안에 여러 번 나눠 합계 `split_min_count`장 이상, 또는 한도 바로 아래
  환불을 `refund_split_window_hours` 안에 `refund_split_min_count`번 이상
- `repeat_refund`: 같은 고객 환불(주문 기준)이 기간 안에 `repeat_refund_min`번 이상
- `duplicate_coupon`: 같은 고객·같은 사건(context_id) 보상 쿠폰이 2번 이상
- `refund_rate_jump`: 환불률(환불액 ÷ 총거래액)이 지난 기간의 `refund_rate_jump_ratio`배 이상이고
  `refund_rate_jump_min_diff` 이상 오름
- `amount_recheck_failed`: 관문을 통과한(executed) 쿠폰·환불을 원천 자료로 다시 계산하니 틀림
  (`recheck.recompute`. 장애 피해 고객 확인은 사후에 자료가 없어 빼고 금액만 본다)
- `gate_hold_trend`: 관문 보류·거절이 기간 안에 `gate_hold_min`건 이상이고 지난 기간의
  `gate_hold_jump_ratio`배 이상
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from ax_contracts.schemas import RequestMeta, VerifyAmountInput
from finance_agent import finance_config as fc
from finance_agent.books import Books, in_range
from finance_agent.closing import compute_totals
from finance_agent.policy import threshold_limits
from finance_agent.recheck import recompute
from finance_agent.sources import Impact

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
RECHECK_FAILURES = {"amount_mismatch", "exceeds_paid", "invalid_params"}


class _BooksSource:
    """사후 재계산용 원천: 결제 금액은 장부, 장애 피해 고객은 사후에 알 수 없으니 None."""

    def __init__(self, paid: dict[str, int]) -> None:
        self.paid = paid

    async def impact(self, meta: RequestMeta, context_id: str, window: Any) -> Impact | None:
        return None

    async def paid_amount(self, meta: RequestMeta, order_id: str) -> int | None:
        return self.paid.get(order_id)

    async def fraud_score(self, meta: RequestMeta, seller_id: str) -> int | None:
        return None


def _finding(code: str, severity: str, title: str, detail: str, refs: list[str]) -> dict:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "detail": detail,
        "refs": [r for r in dict.fromkeys(refs) if r],
    }


def _hhmm(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%m-%d %H:%M")


# ---------------------------------------------------------------- 규칙
def split_coupons(books: Books, start, end, limit: int | None) -> list[dict]:
    cfg = fc.load_config().audit
    if not limit:
        return []
    floor = math.ceil(limit * cfg.near_limit_ratio)
    near = [
        c
        for c in books.coupons
        if isinstance(c.get("unit_value"), int)
        and floor <= c["unit_value"] <= limit  # 한 장 한도(coupon_unit)는 "초과"면 걸린다
        and in_range(c.get("created_at"), start, end)
        and c.get("buyer_id")
    ]
    window = timedelta(minutes=cfg.split_window_minutes)
    out = []
    for buyer in sorted({c["buyer_id"] for c in near}):
        rows = sorted((c for c in near if c["buyer_id"] == buyer), key=lambda c: c["created_at"])
        for i, first in enumerate(rows):
            group = [c for c in rows[i:] if c["created_at"] - first["created_at"] <= window]
            pieces = sum(c.get("count") or 1 for c in group)
            if len(group) >= 2 and pieces >= cfg.split_min_count:
                minutes = int((group[-1]["created_at"] - first["created_at"]).total_seconds() // 60)
                units = sorted({c["unit_value"] for c in group})
                unit_text = "·".join(fc.won(u) for u in units)
                out.append(
                    _finding(
                        "split_under_limit",
                        "high",
                        f"같은 고객에게 한도 바로 아래 쿠폰 {pieces}장",
                        f"{buyer}에게 {max(minutes, 1)}분 안에 {unit_text} 쿠폰 {pieces}장 "
                        f"{len(group)}번 (한 장 한도 {fc.won(limit)})",
                        [c["coupon_id"] for c in group] + [c.get("execution_id") for c in group],
                    )
                )
                break
    return out


def split_refunds(books: Books, start, end, limit: int | None) -> list[dict]:
    cfg = fc.load_config().audit
    if not limit:
        return []
    floor = math.ceil(limit * cfg.near_limit_ratio)
    near = [
        r
        for r in books.refunds
        if floor <= r["amount"] < limit  # 환불 한도(refund_single)는 "이상"이면 걸린다
        and in_range(r.get("created_at"), start, end)
        and r.get("buyer_id")
    ]
    window = timedelta(hours=cfg.refund_split_window_hours)
    out = []
    for buyer in sorted({r["buyer_id"] for r in near}):
        rows = sorted((r for r in near if r["buyer_id"] == buyer), key=lambda r: r["created_at"])
        for i, first in enumerate(rows):
            group = [r for r in rows[i:] if r["created_at"] - first["created_at"] <= window]
            if len(group) >= cfg.refund_split_min_count:
                total = sum(r["amount"] for r in group)
                out.append(
                    _finding(
                        "split_under_limit",
                        "high",
                        f"같은 고객 한도 바로 아래 환불 {len(group)}번",
                        f"{buyer} 환불 {len(group)}번 합계 {fc.won(total)}, "
                        f"건마다 한도 {fc.won(limit)} 바로 아래",
                        [r["refund_id"] for r in group],
                    )
                )
                break
    return out


def repeat_refunds(books: Books, start, end, days: int) -> list[dict]:
    cfg = fc.load_config().audit
    rows = [
        r for r in books.refunds if in_range(r.get("created_at"), start, end) and r.get("buyer_id")
    ]
    out = []
    for buyer in sorted({r["buyer_id"] for r in rows}):
        mine = [r for r in rows if r["buyer_id"] == buyer]
        orders = {r["order_id"] for r in mine}
        if len(orders) >= cfg.repeat_refund_min:
            total = sum(r["amount"] for r in mine)
            out.append(
                _finding(
                    "repeat_refund",
                    "medium",
                    f"같은 고객 환불 {len(orders)}번",
                    f"{buyer}의 주문 {len(orders)}건 환불, 합계 {fc.won(total)} (최근 {days}일)",
                    [r["refund_id"] for r in mine] + sorted(orders),
                )
            )
    return out


def duplicate_coupons(books: Books, start, end) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for c in books.coupons:
        if not in_range(c.get("created_at"), start, end):
            continue
        if (
            c.get("purpose_code") != "compensation"
            or not c.get("context_id")
            or not c.get("buyer_id")
        ):
            continue
        groups.setdefault((c["buyer_id"], c["context_id"]), []).append(c)
    out = []
    for (buyer, ctx), rows in sorted(groups.items()):
        if len(rows) >= 2:
            total = sum(c.get("total_value") or 0 for c in rows)
            out.append(
                _finding(
                    "duplicate_coupon",
                    "medium",
                    f"같은 고객에게 같은 사건 보상 쿠폰 {len(rows)}번",
                    f"{buyer}에게 사건 {ctx} 보상 쿠폰 {len(rows)}번, 합계 {fc.won(total)}",
                    [c["coupon_id"] for c in rows] + [c.get("execution_id") for c in rows],
                )
            )
    return out


def refund_rate_jump(rate: float | None, prev: float | None, days: int) -> list[dict]:
    cfg = fc.load_config().audit
    if rate is None or prev is None:
        return []
    jumped = rate - prev >= cfg.refund_rate_jump_min_diff and (
        prev == 0 or rate >= prev * cfg.refund_rate_jump_ratio
    )
    if not jumped:
        return []
    severity = "high" if prev == 0 or rate >= prev * 2 else "medium"
    return [
        _finding(
            "refund_rate_jump",
            severity,
            "환불률 급변",
            f"최근 {days}일 환불률 {rate * 100:.1f}% (지난 {days}일 {prev * 100:.1f}%)",
            [],
        )
    ]


async def recheck_failures(books: Books, start, end) -> list[dict]:
    if books.executions is None:
        return []
    source = _BooksSource(books.paid_by_order())
    refunded: dict[str, int] = {}
    out = []
    rows = sorted(
        (e for e in books.executions if e.get("status") == "executed"),
        key=lambda e: (e["created_at"], str(e.get("execution_id"))),
    )
    for e in rows:
        action = e.get("action")
        if action not in ("issue_coupon", "refund_request") or e["created_at"] >= end:
            continue
        params = e.get("params") or {}
        meta = RequestMeta(
            context_id=e.get("context_id") or "ctx_audit",
            token="audit",
            deadline=fc.now() + timedelta(seconds=10),
        )
        data = VerifyAmountInput(
            execution_id=str(e.get("execution_id")),
            action=action,
            requester=str(e.get("requester")),
            params=params,
            computed_amount=e.get("computed_amount"),
        )
        result = await recompute(meta, data, source, refunded)
        order_id = str(params.get("order_id")) if params.get("order_id") is not None else None
        if action == "refund_request" and order_id and isinstance(params.get("refund_amount"), int):
            refunded[order_id] = refunded.get(order_id, 0) + params["refund_amount"]
        if not in_range(e["created_at"], start, end) or result.reason_code not in RECHECK_FAILURES:
            continue
        rc = result.recomputed
        if result.reason_code == "exceeds_paid":
            detail = (
                f"{_hhmm(e['created_at'])} 환불 {fc.won(rc.get('refund_amount'))}: 주문 {order_id} "
                f"결제 {fc.won(rc.get('paid_amount'))} 중 이미 {fc.won(rc.get('refunded_before'))} "
                "환불해서 결제 금액을 넘습니다"
            )
        elif result.reason_code == "amount_mismatch":
            detail = (
                f"{_hhmm(e['created_at'])} {action} 요청 총액이 다시 계산한 금액 {rc}과 다릅니다"
            )
        else:
            detail = f"{_hhmm(e['created_at'])} {action} 금액 필드가 잘못되었습니다"
        out.append(
            _finding(
                "amount_recheck_failed",
                "high",
                "관문을 통과한 금액이 다시 계산하면 틀림",
                detail,
                [str(e.get("execution_id")), order_id or ""],
            )
        )
    return out


def gate_trend(
    books: Books, start, end, prev_start, days: int
) -> tuple[list[dict], int | None, int | None]:
    cfg = fc.load_config().audit
    if books.executions is None:
        return [], None, None
    cur = [e for e in books.executions if in_range(e["created_at"], start, end)]
    prev = [e for e in books.executions if in_range(e["created_at"], prev_start, start)]
    held = [e for e in cur if e.get("status") == "held"]
    rejected = [e for e in cur if e.get("status") == "rejected"]
    prev_count = sum(1 for e in prev if e.get("status") in ("held", "rejected"))
    count = len(held) + len(rejected)
    out = []
    if count >= cfg.gate_hold_min and (
        prev_count == 0 or count >= prev_count * cfg.gate_hold_jump_ratio
    ):
        out.append(
            _finding(
                "gate_hold_trend",
                "medium",
                "실행 관문 보류·거절 증가",
                f"최근 {days}일 관문 보류 {len(held)}건·거절 {len(rejected)}건 "
                f"(지난 {days}일 {prev_count}건)",
                [str(e.get("execution_id")) for e in held + rejected][:10],
            )
        )
    return out, len(held), len(rejected)


# ---------------------------------------------------------------- 리포트
async def audit_report(books: Books, days: int = 7) -> tuple[dict[str, Any], list[str]]:
    today = fc.today()
    start = fc.day_start(today - timedelta(days=days - 1))
    end = fc.day_start(today + timedelta(days=1))
    prev_start = start - timedelta(days=days)
    limits = threshold_limits(books)

    refunds = [r for r in books.refunds if in_range(r.get("created_at"), start, end)]
    refund_amount = sum(r["amount"] for r in refunds)
    prev_refund = sum(
        r["amount"] for r in books.refunds if in_range(r.get("created_at"), prev_start, start)
    )
    gmv = compute_totals(books, start, end).gmv
    prev_gmv = compute_totals(books, prev_start, start).gmv
    rate, prev_rate = fc.ratio(refund_amount, gmv), fc.ratio(prev_refund, prev_gmv)
    coupons = [c for c in books.coupons if in_range(c.get("created_at"), start, end)]

    findings = (
        split_coupons(books, start, end, limits.get("coupon_unit"))
        + split_refunds(books, start, end, limits.get("refund_single"))
        + repeat_refunds(books, start, end, days)
        + duplicate_coupons(books, start, end)
        + refund_rate_jump(rate, prev_rate, days)
        + await recheck_failures(books, start, end)
    )
    trend, held, rejected = gate_trend(books, start, end, prev_start, days)
    findings += trend
    findings.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])

    unavailable = []
    if books.executions is None:
        unavailable += ["stats.gate_held", "stats.gate_rejected"]
    if books.thresholds is None:
        unavailable.append("thresholds")
    data = {
        "from": fc.iso(start),
        "to": fc.iso(end),
        "stats": {
            "refund_count": len(refunds),
            "refund_amount": refund_amount,
            "refund_rate": rate,
            "refund_rate_prev": prev_rate,
            "coupon_count": len(coupons),
            "coupon_amount": sum(c.get("total_value") or 0 for c in coupons),
            "gate_held": held,
            "gate_rejected": rejected,
        },
        "findings": findings,
    }
    return data, unavailable
