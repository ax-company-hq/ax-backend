"""결제 문제 진단 skill의 입출력. 읽기이고 결제·환불·취소를 하지 않는다."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel


class DiagnosePaymentInput(AxModel):
    """진단할 주문은 문의에 붙은 주문이다. 다른 주문은 받지 않는다."""

    inquiry_id: int


class PaymentAttemptOut(AxModel):
    payment_id: str
    amount: int
    method_code: str | None = None
    status: str
    failure_code: str | None = None  # 결제 서버가 준 코드 (고객 입력이 아니다)
    created_at: datetime | None = None


class PaymentFacts(AxModel):
    """shop-api 값 그대로와 코드가 센 건수."""

    order_id: str
    order_status: str
    total_amount: int
    attempts: list[PaymentAttemptOut] = Field(default_factory=list)
    succeeded_count: int = 0
    failed_count: int = 0
    last_failure_code: str | None = None


Situation = Literal[
    "paid",  # 결제가 정상으로 끝났다
    "payment_failed",  # 결제가 실패했고 아직 안 낸 상태다
    "not_attempted",  # 주문만 만들고 결제 시도가 없다
    "duplicate_payment",  # 성공한 결제가 둘 이상이다
    "inconsistent",  # 주문 상태와 결제 기록이 맞지 않는다
    "cancelled",  # 주문이 취소됐다
    "other",
]

NextAction = Literal[
    "none",  # 할 일 없음
    "retry_payment",  # 고객이 같은 주문으로 결제를 다시 시도한다
    "wait_and_retry",  # 결제 서버 장애로 보여 잠시 뒤 다시 시도한다
    "complete_payment",  # 고객이 결제를 이어서 한다
    "review_refund",  # 담당자가 중복 결제를 확인해 환불을 검토한다
    "handoff_review",  # 담당자가 직접 확인한다
]


class PaymentDiagnosis(AxModel):
    """`message`는 코드가 만든 사실 문장이다 (고객 원문이나 LLM 문장이 아니다)."""

    situation: Situation
    next_action: NextAction
    message: str
    likely_cause: Literal["payment_service_fault", "payment_declined", "unknown"] | None = None
    duplicate_amount: int = 0  # 중복으로 보이는 금액 (성공 결제 중 첫 건을 뺀 합)
    facts: PaymentFacts | None = None
    notes: list[str] = Field(default_factory=list)
