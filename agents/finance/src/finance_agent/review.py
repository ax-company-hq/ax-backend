"""재무 AI 1차 판단 (`review_hold`): 사람 확인으로 넘어온 건을 사람보다 먼저 결정한다.

판단은 코드 규칙(`ai_review_policy.toml`)과 원천 데이터로만 한다. LLM은 숫자나 결론을 정하지 않는다.
결과는 세 가지다.

- approve: 기준은 넘었지만 재량 한도 안이고 근거가 맞음
- reject: 근거가 틀림 (피해 고객이 아님, 결제보다 큰 환불, 위험 점수가 매우 높음, 금액 계산이 다름)
- escalate: 한도를 넘는 큰 금액이거나 판단할 자료가 없음 → 사람이 결정

설명(`explanation`)은 사람이 읽을 쉬운 문장이고, 숫자는 모두 코드가 계산한 값이다.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ax_contracts.schemas import RequestMeta
from finance_agent.schemas import ReviewHoldInput, ReviewHoldOutput
from finance_agent.sources import Sources, order_key


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CouponPolicy(_Strict):
    max_total: int = Field(ge=0)
    max_unit: int = Field(ge=0)
    max_customer_total: int = Field(ge=0)
    max_daily_total: int = Field(ge=0)
    max_compensation_to_damage: float = Field(gt=0)


class RefundPolicy(_Strict):
    max_amount: int = Field(ge=0)


class SellerPolicy(_Strict):
    reject_risk_at: int = Field(ge=0, le=100)


class ReviewPolicy(_Strict):
    coupon: CouponPolicy
    refund: RefundPolicy
    seller: SellerPolicy


def load_policy() -> ReviewPolicy:
    text = resources.files("finance_agent").joinpath("ai_review_policy.toml").read_text("utf-8")
    return ReviewPolicy.model_validate(tomllib.loads(text))


def _won(n: int) -> str:
    return f"{n:,}원"


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _out(decision: str, reason: str, explanation: str) -> ReviewHoldOutput:
    return ReviewHoldOutput(decision=decision, reason_code=reason, explanation=explanation)  # type: ignore[arg-type]


async def decide(
    data: ReviewHoldInput,
    meta: RequestMeta,
    sources: Sources,
    refunded_before: int,
    policy: ReviewPolicy,
) -> ReviewHoldOutput:
    if data.action == "issue_coupon":
        return await _coupon(data, meta, sources, policy.coupon)
    if data.action == "refund_request":
        return await _refund(data, meta, sources, refunded_before, policy.refund)
    if data.action == "seller_review":
        return _seller(data, policy.seller)
    return _out(
        "escalate", "not_finance_domain", "재무 AI가 판단하는 일이 아니라 사람에게 넘깁니다."
    )


async def _coupon(
    data: ReviewHoldInput, meta: RequestMeta, sources: Sources, p: CouponPolicy
) -> ReviewHoldOutput:
    count, unit = _int(data.params.get("count")), _int(data.params.get("unit_value"))
    if not count or not unit:
        return _out("reject", "invalid_params", "쿠폰 장수나 금액이 잘못되어 거절합니다.")
    amount = count * unit  # 계산은 코드로
    if data.computed_amount is not None and data.computed_amount != amount:
        return _out(
            "reject",
            "amount_mismatch",
            f"다시 계산한 금액 {_won(amount)}이 안전 장치 계산과 달라 거절합니다.",
        )
    compensation = data.params.get("purpose_code") == "compensation"
    buyer = data.params.get("buyer_id")

    impact = None
    if compensation:
        impact = await sources.impact(meta, meta.context_id, None)
        if impact is None or not impact.affected_customer_ids:
            return _out(
                "escalate",
                "no_impact_data",
                "장애 피해 자료를 받지 못해 보상이 맞는지 판단할 수 없습니다. "
                "사람이 확인해 주세요.",
            )
        if buyer not in impact.affected_customer_ids:
            return _out(
                "reject",
                "not_affected",
                f"{buyer}는 이 장애의 피해 고객 목록에 없어 보상을 거절합니다.",
            )

    if data.hold_reason == "verify_timeout":  # 처음 확인 때 답하지 못한 건을 지금 다시 확인했다
        return _out(
            "approve",
            "rechecked",
            f"다시 확인해 보니 {_won(amount)} 계산과 받는 사람이 맞아 승인합니다.",
        )

    over: list[str] = []
    totals = data.totals
    if amount > p.max_total:
        over.append(f"한 번에 {_won(amount)} (재량 한도 {_won(p.max_total)})")
    if unit > p.max_unit:
        over.append(f"한 장 {_won(unit)} (재량 한도 {_won(p.max_unit)})")
    daily = _int(totals.get("daily_total"))
    if daily is not None and daily > p.max_daily_total:
        over.append(f"오늘 쿠폰 합계 {_won(daily)} (재량 한도 {_won(p.max_daily_total)})")
    customer_total = _int(totals.get("customer_total"))
    if compensation and customer_total is not None and customer_total > p.max_customer_total:
        over.append(
            f"한 고객 보상 합계 {_won(customer_total)} (재량 한도 {_won(p.max_customer_total)})"
        )
    if over:
        return _out(
            "escalate",
            "over_ai_limit",
            "재무 AI가 혼자 정하기엔 큰 금액이라 사람에게 넘깁니다: " + ", ".join(over) + ".",
        )

    if compensation and impact is not None:
        context_total = _int(totals.get("context_total")) or amount
        limit = int(impact.lost_amount * p.max_compensation_to_damage)
        if context_total > limit:
            return _out(
                "escalate",
                "compensation_exceeds_damage",
                f"이 장애의 보상 합계 {_won(context_total)}가 "
                f"피해 금액 {_won(impact.lost_amount)}을 넘습니다. 사람이 확인해 주세요.",
            )
        return _out(
            "approve",
            "within_ai_limit",
            f"피해 고객 {buyer}에게 {_won(amount)} 보상. 기준은 넘지만 재무 AI 재량 한도 안이고, "
            f"이 장애의 보상 합계 {_won(context_total)}가 피해 금액 {_won(impact.lost_amount)}보다 "
            "작아 승인합니다.",
        )
    return _out(
        "approve",
        "within_ai_limit",
        f"{_won(amount)} 쿠폰. 기준은 넘지만 재무 AI 재량 한도 안이라 승인합니다.",
    )


async def _refund(
    data: ReviewHoldInput,
    meta: RequestMeta,
    sources: Sources,
    refunded_before: int,
    p: RefundPolicy,
) -> ReviewHoldOutput:
    amount, order_id = (
        _int(data.params.get("refund_amount")),
        order_key(data.params.get("order_id")),
    )
    if not amount or order_id is None:
        return _out("reject", "invalid_params", "환불 금액이나 주문 번호가 잘못되어 거절합니다.")
    if amount > p.max_amount:
        return _out(
            "escalate",
            "over_ai_limit",
            f"환불 {_won(amount)}은 재무 AI 재량 한도({_won(p.max_amount)})를 넘어 "
            "사람에게 넘깁니다.",
        )
    paid = await sources.paid_amount(meta, order_id)
    if paid is None:
        return _out(
            "escalate",
            "no_payment_data",
            f"{order_id}의 결제 금액을 확인할 수 없어 사람에게 넘깁니다.",
        )
    if refunded_before + amount > paid:
        return _out(
            "reject",
            "exceeds_paid",
            f"{order_id} 결제 {_won(paid)} 중 이미 {_won(refunded_before)}을 환불해서 "
            f"{_won(amount)}을 더 환불할 수 없습니다.",
        )
    return _out(
        "approve",
        "within_ai_limit",
        f"{order_id} 결제 {_won(paid)} 안의 환불 {_won(amount)}이라 승인합니다.",
    )


def _seller(data: ReviewHoldInput, p: SellerPolicy) -> ReviewHoldOutput:
    risk = _int(data.params.get("risk_score"))
    if risk is None:
        return _out("escalate", "no_risk_score", "위험 점수가 없어 사람에게 넘깁니다.")
    if risk >= p.reject_risk_at:
        return _out(
            "reject",
            "high_risk",
            f"위험 점수 {risk}점은 매우 높아(기준 {p.reject_risk_at}점 이상) 입점을 거절합니다.",
        )
    return _out(
        "escalate",
        "needs_human_check",
        f"위험 점수 {risk}점은 애매한 구간이라 사람이 판매자 정보를 확인해 주세요.",
    )
