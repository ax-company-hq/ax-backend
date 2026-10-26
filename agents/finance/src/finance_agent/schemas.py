"""finance skill 입출력 (재무 구성안 4-2, 공통 구성안 12장, 재무 재설계 2026-10-23 7장).

`verify_amount`·`review_hold` skill은 재무 재설계로 없앴다. 금액 다시 계산 규칙은
`recheck.py`에 남아 있고 형식은 contracts 원본(`VerifyAmountInput`·`VerifyAmountOutput`)을 쓴다.
금액은 모두 정수 원, 시각은 UTC ISO8601 문자열이다.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import Field

from ax_contracts.schemas import AxModel, UtcDatetime


# ---------------------------------------------------------------- loss_aggregation (손실 리포트)
class LossAggregationInput(AxModel):
    """장애 구간. `context_id`가 없으면 요청 meta의 사건 번호를 쓴다."""

    window_from: UtcDatetime
    window_to: UtcDatetime
    context_id: str | None = None
    category: str | None = None


class LossReport(AxModel):
    """손실 리포트. 영향 숫자는 data `impact_analysis` 결과를 그대로 옮기고 다시 계산하지 않는다.

    받지 못한 값은 None("확인할 수 없음")이고, 그때 `total_loss`도 None이다.
    """

    context_id: str
    failed_orders: int | None = None
    affected_customers: int | None = None
    lost_amount: int | None = None
    compensation_coupon_count: int
    compensation_total: int
    total_loss: int | None = None
    summary: str


# ---------------------------------------------------- 마켓운영: seller_review, settlement_hold
class SellerReviewInput(AxModel):
    """판매자 심사. 판매자 소개 문장은 `untrusted`로만 오고 판단에 쓰지 않는다."""

    seller_id: str
    category_codes: list[str] = Field(default_factory=list)
    documents_verified: bool


class SellerReviewOutput(AxModel):
    seller_id: str
    decision: Literal["approved", "held", "rejected", "undetermined"]
    risk_score: int | None = None
    reason_code: str
    execution_id: str | None = None
    review_id: str | None = None


class SettlementHoldInput(AxModel):
    seller_id: str
    reason_code: Literal["fraud_risk", "dispute", "documents_missing", "reconciliation_mismatch"]
    hold_amount: int | None = Field(default=None, ge=0)


class SettlementHoldOutput(AxModel):
    seller_id: str
    status: Literal["held", "pending_review", "rejected"]
    reason_code: str
    execution_id: str | None = None
    review_id: str | None = None


# ---------------------------------------------- closing_report (orchestrator·data → finance, 읽기)
class ClosingReportInput(AxModel):
    """기간 손익 요약. `date`가 없으면 오늘(UTC), `month`면 그 달."""

    period: Literal["day", "month"] = "day"
    date: dt.date | None = None


class ClosingLine(AxModel):
    code: str
    label: str
    amount: int


class JournalLine(AxModel):
    account: str
    debit: int = Field(ge=0)
    credit: int = Field(ge=0)
    memo: str


class ClosingReportOutput(AxModel):
    """숫자는 코드가 계산한다. `commentary`만 LLM이 쓸 수 있다(`commentary_source`)."""

    period: Literal["day", "month"]
    date: str
    window_from: str
    window_to: str
    lines: list[ClosingLine]
    journal: list[JournalLine]
    commentary: str
    commentary_source: Literal["llm", "template"]
    status: Literal["draft", "confirmed"]
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    unavailable: list[str] = Field(default_factory=list)


# ------------------------------------------------ audit_report (orchestrator·data → finance, 읽기)
class AuditReportInput(AxModel):
    days: int = Field(default=7, ge=1, le=90)


class AuditFinding(AxModel):
    code: Literal[
        "split_under_limit",
        "repeat_refund",
        "duplicate_coupon",
        "refund_rate_jump",
        "amount_recheck_failed",
        "gate_hold_trend",
    ]
    severity: Literal["high", "medium", "low"]
    title: str
    detail: str
    refs: list[str] = Field(default_factory=list)


class AuditStats(AxModel):
    refund_count: int
    refund_amount: int
    refund_rate: float | None = None
    refund_rate_prev: float | None = None
    coupon_count: int
    coupon_amount: int
    gate_held: int | None = None
    gate_rejected: int | None = None


class AuditReportOutput(AxModel):
    window_from: str
    window_to: str
    stats: AuditStats
    findings: list[AuditFinding]
    unavailable: list[str] = Field(default_factory=list)
