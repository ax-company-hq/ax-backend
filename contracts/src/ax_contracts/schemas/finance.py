"""finance skill 중 다른 부서가 함께 쓰는 입출력 (재무 구성안 4-2, 공통 구성안 12장).

`verify_amount`는 안전 레이어(safety)가 B 액션의 금액을 finance에 교차검증할 때 쓴다.
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
