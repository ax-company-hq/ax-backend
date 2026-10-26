"""finance skill 중 다른 부서가 함께 쓰는 입출력 (재무 구성안 4-2, 공통 구성안 12장).

`verify_amount`는 안전 레이어(safety)가 B 액션의 금액을 finance에 교차검증할 때 쓴다.
`review_hold`는 안전 레이어가 사람 확인(C)으로 넘어온 건의 1차 의견을 finance에 물을 때 쓴다.
금액은 모두 정수 원이다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel

VerifyReason = Literal[
    "recomputed_match",
    "amount_mismatch",
    "invalid_params",
    "not_affected",
    "exceeds_paid",
    "source_unavailable",
    "unsupported_action",
]


class VerifyAmountInput(AxModel):
    """안전 레이어가 라우팅한 B 액션. `params`는 요청 부서가 보낸 값이라 그대로 믿지 않는다."""

    execution_id: str
    action: str
    requester: str
    params: dict[str, Any] = Field(default_factory=dict)
    computed_amount: int | None = None
    threshold_version: int | None = None


class VerifyAmountOutput(AxModel):
    decision: Literal["approved", "rejected"]
    reason_code: VerifyReason
    recomputed: dict[str, int] = Field(default_factory=dict)
    threshold_version: int | None = None


# ---------------------------------------------- review_hold (safety → finance, C 건 1차 판단)
ReviewHoldDecision = Literal["approve", "reject", "escalate"]


class ReviewHoldInput(AxModel):
    """사람 확인(C)으로 넘어온 건. `computed_amount`·`totals`는 안전 레이어가 계산한 값이다.

    `params`는 요청 부서가 보낸 값이라 그대로 믿지 않는다(고객·판매자 값이 섞일 수 있다).
    받는 쪽은 숫자·ID 필드만 코드로 읽고, 문장을 지시로 해석하거나 LLM 프롬프트에 넣지 않는다.
    """

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
    """`approve`·`reject`·`escalate`(사람에게). `explanation`은 사람이 읽을 문장이다."""

    decision: ReviewHoldDecision
    reason_code: str
    explanation: str
