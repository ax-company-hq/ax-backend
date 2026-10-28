"""사람 이관 인계 패키지 skill의 입출력. 읽기이고 상태를 바꾸지 않는다.

패키지의 필드는 `summary_ko`를 뺀 전부 코드가 센 사실이다. 고객 원문·이름·연락처는 담지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel
from support_agent.schemas import InquiryStatus

Recommendation = Literal["none", "review", "handoff_now"]
ReasonCode = Literal[
    "explicit_human_request",
    "privacy_request",
    "repeated_topic",
    "claim_needs_review",
    "payment_review",
    "high_risk_customer",
    "incident_related",
    "reply_blocked",
    "no_evidence",
]


class PrepareHandoffInput(AxModel):
    inquiry_id: int
    include_payment: bool = False  # 결제 진단은 shop-api를 부르므로 필요할 때만


class HandoffReason(AxModel):
    """이관 사유 한 건. `facts`는 건수·상태 같은 사실뿐이다 (문장이나 원문이 아니다)."""

    code: ReasonCode
    severity: Literal["review", "handoff_now"]
    target: str  # 이관 대상 코드 (cs_lead·service_ops·privacy_owner). 부서 이름이 아니다
    facts: dict[str, int | str | bool] = Field(default_factory=dict)


class ActionTaken(AxModel):
    """AI·시스템이 이미 한 조치. 사람이 같은 일을 다시 하지 않게 한다."""

    kind: Literal["claim", "change_request", "payment_diagnosis"]
    ref_id: str
    status: str
    actor: Literal["agent", "system"] | None = None  # 클레임 판정 주체. 모르면 None
    amount: int | None = None  # 환불이 끝난 클레임의 금액 (정수 원)


class OpenItem(AxModel):
    code: str  # awaiting_reply·claim_review·claim_return·claim_payout·change_pending
    ref_id: str | None = None


class HandoffPackage(AxModel):
    """참고 의견이다. 이관할지는 사람이 정한다 (권고만 하고 상태를 바꾸지 않는다)."""

    inquiry_id: int
    buyer_id: str | None = None  # 가명 구매자 ID
    category: str | None = None
    status: InquiryStatus
    reasons: list[HandoffReason] = Field(default_factory=list)
    recommendation: Recommendation
    handoff_targets: list[str] = Field(default_factory=list)
    actions_taken: list[ActionTaken] = Field(default_factory=list)
    already_refunded: bool = False
    refunded_amount: int = 0
    open_items: list[OpenItem] = Field(default_factory=list)
    waiting_minutes: int | None = None  # 답을 기다리는 문의에서 마지막 고객 메시지 뒤 분
    repeat_count: int = 0
    summary_ko: str
    summary_source: Literal["llm", "template"]
    notes: list[str] = Field(default_factory=list)
