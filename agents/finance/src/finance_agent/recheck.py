"""금액 다시 계산 (순수 규칙). 예전 `verify_amount` skill의 계산 코드다.

재무 재설계(2026-10-23)로 건별 교차검증 skill은 없어졌다. 같은 계산을 이제 사후 감사
(`audit.py`, `amount_recheck_failed`)가 쓴다: 관문을 통과한 쿠폰·환불을 원천 자료로 다시 계산한다.

입출력 형식(`VerifyAmountInput`·`VerifyAmountOutput`)은 contracts 원본을 그대로 쓴다.
근거가 없으면 통과시키지 않는다(`source_unavailable`). 상태를 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Any

from ax_contracts.schemas import RequestMeta, VerifyAmountInput, VerifyAmountOutput
from finance_agent.sources import Sources, order_key

CLAIMED_TOTAL_FIELDS = ("total_value", "claimed_total")

__all__ = ["CLAIMED_TOTAL_FIELDS", "VerifyAmountInput", "VerifyAmountOutput", "recompute"]


def _positive_int(value: Any) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    return value if ok else None


async def recompute(
    meta: RequestMeta,
    data: VerifyAmountInput,
    sources: Sources,
    refunds: dict[str, int],
) -> VerifyAmountOutput:
    """원천 자료(`sources`)와 이미 환불한 금액(`refunds`, 주문별)으로 다시 계산한다."""

    def reject(reason: str, **recomputed: int) -> VerifyAmountOutput:
        return VerifyAmountOutput(
            decision="rejected",
            reason_code=reason,  # type: ignore[arg-type]
            recomputed=recomputed,
            threshold_version=data.threshold_version,
        )

    params = data.params
    if data.action == "issue_coupon":
        count, unit = _positive_int(params.get("count")), _positive_int(params.get("unit_value"))
        if count is None or unit is None:
            return reject("invalid_params")
        total = count * unit
        claimed = [params.get(k) for k in CLAIMED_TOTAL_FIELDS if params.get(k) is not None]
        if data.computed_amount is not None:
            claimed.append(data.computed_amount)
        if any(c != total for c in claimed):
            return reject("amount_mismatch", count=count, unit_value=unit, total_value=total)
        if params.get("purpose_code") == "compensation":
            impact = await sources.impact(meta, meta.context_id, None)
            if impact is None or not impact.affected_customer_ids:
                return reject("source_unavailable", total_value=total)
            if params.get("buyer_id") not in impact.affected_customer_ids:
                return reject("not_affected", total_value=total)
        return VerifyAmountOutput(
            decision="approved",
            reason_code="recomputed_match",
            recomputed={"count": count, "unit_value": unit, "total_value": total},
            threshold_version=data.threshold_version,
        )

    if data.action == "refund_request":
        amount, order_id = (
            _positive_int(params.get("refund_amount")),
            order_key(params.get("order_id")),
        )
        if amount is None or order_id is None:
            return reject("invalid_params")
        if data.computed_amount is not None and data.computed_amount != amount:
            return reject("amount_mismatch", refund_amount=amount)
        paid = await sources.paid_amount(meta, order_id)
        if paid is None:
            return reject("source_unavailable", refund_amount=amount)
        already = refunds.get(order_id, 0)
        if already + amount > paid:
            return reject(
                "exceeds_paid", refund_amount=amount, paid_amount=paid, refunded_before=already
            )
        return VerifyAmountOutput(
            decision="approved",
            reason_code="recomputed_match",
            recomputed={"refund_amount": amount, "paid_amount": paid, "refunded_before": already},
            threshold_version=data.threshold_version,
        )

    return reject("unsupported_action")
