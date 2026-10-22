"""클레임(취소·반품·교환) skill의 입력·출력 모델.

금액은 정수 원이고 모두 코드가 주문 사실에서 계산한 값이다. 입력에는 금액이 없다.
사유 설명·메모 같은 자유 문장은 `AgentRequest.untrusted`로만 받는다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel

ClaimType = Literal["cancel", "return", "exchange"]
ClaimReason = Literal[
    "change_of_mind", "defect", "wrong_item", "damaged", "missing_item", "late_delivery", "other"
]
ClaimStatus = Literal[
    "needs_info",
    "needs_review",
    "awaiting_return",
    "return_received",
    "approved",
    "completed",
    "rejected",
]
Inspection = Literal["pass", "fail", "partial"]


class ClaimReasonHit(AxModel):
    code: str
    level: Literal["block", "review", "warn", "info"]
    message: str
    rule: str | None = None


class ClaimLine(AxModel):
    order_item_id: int
    title: str
    qty: int
    requested_qty: int
    unit_price: int
    refund_cash: int
    points_back: int


class ClaimDeduction(AxModel):
    code: str
    amount: int
    message: str


class ClaimAudit(AxModel):
    actor: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
    at: str


class Claim(AxModel):
    id: int
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str
    order_id: int
    order_no: str
    user_id: int
    customer: str
    type: ClaimType
    reason: ClaimReason
    status: ClaimStatus
    created_at: str
    updated_at: str
    outcome: str
    resolution: str
    requires_return: bool
    evidence_count: int = 0
    unsealed: bool = False
    conversation_id: int | None = None
    reasons: list[ClaimReasonHit] = Field(default_factory=list)
    lines: list[ClaimLine] = Field(default_factory=list)
    deductions: list[ClaimDeduction] = Field(default_factory=list)
    shipping_refund: int = 0
    expected_refund: int = 0
    points_back: int = 0
    abuse_score: int = 0
    inspection: Inspection | None = None
    return_received_at: str | None = None
    refund_paid: int = 0
    processed_at: str | None = None
    decided_by: str | None = None
    audit: list[ClaimAudit] = Field(default_factory=list)


class ClaimStats(AxModel):
    total: int
    by_status: dict[str, int]
    rule_hits: dict[str, int]


class QueueEntry(AxModel):
    position: int
    claim_id: int
    order_no: str
    customer: str
    type: ClaimType
    reason: ClaimReason
    inspection: Inspection | None = None
    amount: int
    ready: bool
    blockers: list[ClaimReasonHit] = Field(default_factory=list)


class ClaimQueue(AxModel):
    queue: list[QueueEntry]
    awaiting_return: list[Claim]


class QueueRunResult(AxModel):
    claim_id: int
    result: Literal["paid", "held", "skipped"]
    detail: str | None = None
    refund: int | None = None


class QueueRun(AxModel):
    results: list[QueueRunResult]
    paid: int
    held: int
    queue: list[QueueEntry]


class ClaimItemRequest(AxModel):
    order_item_id: int
    qty: int = Field(ge=1)


class CreateClaimInput(AxModel):
    """클레임 접수. 사유 설명은 `untrusted.description`으로 받는다."""

    user_id: int
    customer_name: str
    order_id: int
    type: ClaimType
    reason: ClaimReason
    items: list[ClaimItemRequest] | None = None  # 없으면 주문 전체
    unsealed: bool = False
    evidence_count: int = Field(default=0, ge=0)
    conversation_id: int | None = None


class ListClaimsInput(AxModel):
    status: ClaimStatus | None = None


class GetClaimInput(AxModel):
    claim_id: int


class DecideClaimInput(AxModel):
    """상담원 결정. 메모는 `untrusted.note`로 받는다."""

    claim_id: int
    decision: Literal["approve", "reject"]


class ReceiveReturnInput(AxModel):
    claim_id: int
    inspection: Inspection | None = None


class InspectReturnInput(AxModel):
    claim_id: int
    inspection: Inspection


class ProcessClaimQueueInput(AxModel):
    limit: int | None = Field(default=None, ge=1)
    claim_ids: list[int] | None = None
