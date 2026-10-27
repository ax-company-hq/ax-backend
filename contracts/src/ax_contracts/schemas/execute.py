"""실행 관문 `execute()`의 입출력 (공통 구성안 7장). 인터페이스만 둔다.

상태를 바꾸는 모든 액션은 이 관문 하나로만 일어난다. 등급은 규칙(레지스트리 기본 등급과
금액 기준 중 높은 쪽)이 정하고 LLM이 정하지 않는다. 구현은 안전 레이어(태윤)가 한다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel
from ax_contracts.schemas.message import Evidence


class HoldReason(StrEnum):
    """C 등급으로 보류하거나 거부한 이유."""

    OVER_THRESHOLD = "over_threshold"
    UNREGISTERED_ACTION = "unregistered_action"
    AMOUNT_MISMATCH = "amount_mismatch"
    VERIFY_REJECTED = "verify_rejected"
    VERIFY_TIMEOUT = "verify_timeout"
    SCHEMA_INVALID = "schema_invalid"
    REVIEW_TIMEOUT = "review_timeout"


class ExecuteRequest(AxModel):
    """실행 관문으로 보내는 요청.

    `claimed_grade`는 요청 쪽이 주장한 등급이다. 기록만 하고 판정에는 쓰지 않는다.
    금액은 안전 레이어가 다시 계산하며, 요청 값과 다르면 C(`amount_mismatch`)가 된다.
    """

    action: str
    requester: str
    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    idempotency_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence = Field(default_factory=Evidence)
    claimed_grade: str | None = None


class ExecuteDecision(AxModel):
    """실행 관문의 판정 결과.

    - `status`: `executed`(실행함), `verifying`(B: 교차검증 중), `held`(C: 보류 큐),
      `rejected`(거부), `failed`(실행 오류)
    - `computed_amount`·`threshold_version`: 안전 레이어가 계산한 금액과 기준표 버전
    - `review_id`: C 보류 건의 승인 대기열 ID
    - 같은 `idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다.
    """

    execution_id: str
    grade: Literal["A", "B", "C"]
    status: Literal["executed", "verifying", "held", "rejected", "failed"]
    computed_amount: int | None = None
    threshold_version: int | None = None
    verifier: str | None = None
    hold_reason: HoldReason | None = None
    review_id: str | None = None
    result_ref: str | None = None

    @model_validator(mode="after")
    def _held_needs_reason(self) -> ExecuteDecision:
        if self.status == "held" and self.hold_reason is None:
            raise ValueError("held에는 hold_reason이 필요하다")
        return self
