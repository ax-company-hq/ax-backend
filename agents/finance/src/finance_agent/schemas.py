"""finance skill 입출력 (재무 구성안 4-2, 공통 구성안 12장).

TODO: 다른 부서가 함께 쓰는 형식(`VerifyAmountInput`·`VerifyAmountOutput`은 safety가 보낸다)은
`contracts/`로 옮긴다 (to:contracts 요청). 그 전까지 safety가 보내는 필드와 여기를 같게 유지한다.
금액은 모두 정수 원이다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ax_contracts.schemas import AxModel, UtcDatetime


# ---------------------------------------------------------------- verify_amount (safety → finance)
class VerifyAmountInput(AxModel):
    """안전 레이어가 라우팅한 B 액션. `params`는 요청 부서가 보낸 값이라 그대로 믿지 않는다."""

    execution_id: str
    action: str
    requester: str
    params: dict[str, Any] = Field(default_factory=dict)
    computed_amount: int | None = None
    threshold_version: int | None = None


VerifyReason = Literal[
    "recomputed_match",
    "amount_mismatch",
    "invalid_params",
    "not_affected",
    "exceeds_paid",
    "source_unavailable",
    "unsupported_action",
]


class VerifyAmountOutput(AxModel):
    decision: Literal["approved", "rejected"]
    reason_code: VerifyReason
    recomputed: dict[str, int] = Field(default_factory=dict)
    threshold_version: int | None = None


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
