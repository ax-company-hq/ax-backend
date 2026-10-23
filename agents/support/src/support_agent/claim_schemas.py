"""클레임(취소·반품·교환) skill의 입력·출력 모델.

금액은 정수 원이고 모두 코드가 주문 사실에서 계산한 값이다. 입력에는 금액이 없다.
사유 설명·메모 같은 자유 문장은 `AgentRequest.untrusted`로만 받는다.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, model_validator

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel


def _legacy_id(value: Any) -> Any:
    """숫자 ID로 저장된 예전 기록을 문자열로 읽는다 (주문·품목 ID가 문자열로 바뀌기 전 DB 기록)."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


# 주문·품목 ID는 문자열이다 (shop-api는 `ord_…`). 저장된 예전 숫자 기록도 읽는다.
EntityId = Annotated[str, BeforeValidator(_legacy_id)]

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
    order_item_id: EntityId
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


class ClaimReceiptItem(AxModel):
    id: EntityId
    title: str
    qty: int
    paid_amount: int
    points_share: int = 0
    refunded_qty: int = 0


class ClaimReceipt(AxModel):
    """판정에 쓴 주문 사실의 스냅샷. 나중에 주문이 바뀌어도 판정 근거를 그대로 보여 준다."""

    order_no: str
    status: str
    items_total: int
    shipping_fee: int
    points_used: int
    paid_amount: int
    paid_at: str
    delivered_at: str | None = None
    items: list[ClaimReceiptItem] = Field(default_factory=list)


class Claim(AxModel):
    id: int
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str
    order_id: EntityId
    order_no: str
    buyer_id: str  # 구매자 ID (shop-api `buyer_id`, 예: usr_1)
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
    agent_note: str | None = None  # 상담원 결정 메모 (`untrusted.note`)
    inspection_note: str | None = None  # 검수 메모 (`untrusted.note`)
    receipt: ClaimReceipt | None = None
    audit: list[ClaimAudit] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _read_legacy_buyer(cls, data: Any) -> Any:
        """숫자 `user_id`로 저장된 예전 기록은 `buyer_id`(`usr_<번호>`)로 읽는다."""
        if isinstance(data, dict) and "buyer_id" not in data and "user_id" in data:
            data = dict(data)
            legacy = data.pop("user_id")
            data["buyer_id"] = f"usr_{legacy}" if isinstance(legacy, int) else str(legacy)
        return data


class ClaimStats(AxModel):
    total: int
    by_status: dict[str, int]
    rule_hits: dict[str, int]
    agent_reviews: int = 0  # 상담원이 승인·거절을 결정한 클레임 수
    # 상담원이 규칙 판정을 뒤집은 수. 사람 결정은 `needs_review`·`needs_info`에서만 받고
    # 자동 판정(자동 거절·자동 승인)은 바꿀 수 없어서 지금은 항상 0이다.
    agent_overrides: int = 0


class QueueItemLine(AxModel):
    title: str
    qty: int


class QueueEntry(AxModel):
    position: int
    claim_id: int
    order_no: str
    customer: str
    type: ClaimType
    reason: ClaimReason
    received_at: str | None = None  # 반품 입고 시각 (반품 없는 승인은 승인 시각)
    inspection: Inspection | None = None
    amount: int
    ready: bool
    blockers: list[ClaimReasonHit] = Field(default_factory=list)
    items: list[QueueItemLine] = Field(default_factory=list)


class ClaimQueue(AxModel):
    queue: list[QueueEntry]
    awaiting_return: list[Claim]


class QueueRunResult(AxModel):
    claim_id: int
    result: Literal["paid", "held", "skipped"]
    detail: str | None = None
    blockers: list[ClaimReasonHit] = Field(default_factory=list)
    refund: int | None = None


class QueueRun(AxModel):
    results: list[QueueRunResult]
    paid: int
    held: int
    queue: list[QueueEntry]


class ClaimItemRequest(AxModel):
    order_item_id: str
    qty: int = Field(ge=1)


class CreateClaimInput(AxModel):
    """클레임 접수. 사유 설명은 `untrusted.description`으로 받는다."""

    buyer_id: str  # 구매자 ID (usr_…). 주문의 구매자와 같아야 한다
    customer_name: str
    order_id: str  # 주문 ID (shop-api는 ord_…)
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
