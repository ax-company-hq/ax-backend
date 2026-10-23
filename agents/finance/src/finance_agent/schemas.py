"""finance skill 입출력 (재무 구성안 4-2, 공통 구성안 12장).

다른 부서와 함께 쓰는 `VerifyAmountInput`·`VerifyAmountOutput`·`VerifyReason`은
contracts가 원본이다.
금액은 모두 정수 원이다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ax_contracts import schemas as _contract
from ax_contracts.schemas import AxModel, UtcDatetime

# ---------------------------------------------------------------- verify_amount (safety → finance)
# 안전 레이어와 함께 쓰는 형식은 contracts가 원본이다(#82, #92). 여기서는 다시 내보내기만 한다.
VerifyAmountInput = _contract.VerifyAmountInput
VerifyAmountOutput = _contract.VerifyAmountOutput
VerifyReason = _contract.VerifyReason


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


# -------------------------------------------- review_hold (safety → finance, AI 1차 판단)
class ReviewHoldInput(AxModel):
    """사람 확인(C)으로 넘어온 건. 금액·누적은 안전 장치가 계산한 값이다."""

    review_id: str
    execution_id: str
    action: str
    requester: str
    params: dict[str, Any] = Field(default_factory=dict)
    computed_amount: int | None = None
    hold_reason: str
    rules: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, int | None] = Field(default_factory=dict)


class ReviewHoldOutput(AxModel):
    decision: Literal["approve", "reject", "escalate"]
    reason_code: str
    explanation: str


# ---------------------------------- review_threshold_change (safety → finance, 기준 바꾸기 판단)
class ThresholdReviewInput(AxModel):
    """안전 장치 범위 안의 기준 바꾸기 요청. 숫자·`stats`는 safety가 코드로 센 값이다.

    요청한 사람이 쓴 이유는 `untrusted.reason_untrusted_text`로 오며 판단에 쓰지 않는다.
    """

    change_id: str
    key: str
    current_value: int | None = None
    new_value: int
    baseline: int | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


class ThresholdReviewOutput(AxModel):
    decision: Literal["approve", "reject", "escalate"]
    reason_code: str
    explanation: str
