"""상담원 화면(dashboard-web)이 받는 응답 모델.

dashboard-web `src/lib/types.ts`와 필드 이름·형태를 같게 맞췄다. 이 모델에서 OpenAPI가 만들어지고
(`contracts/openapi/dashboard-api.json`), 프론트 타입은 그 파일에서 생성한다.
시간은 UTC ISO8601 문자열, 금액은 정수 원이다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Urgency = Literal["low", "medium", "high"]
ConversationStatus = Literal["open", "resolved", "escalated"]
ClaimType = Literal["cancel", "return", "exchange", "compensation"]
ClaimReason = Literal[
    "change_of_mind", "defect", "wrong_item", "damaged", "late_delivery", "missing_item", "other"
]
ClaimStatus = Literal[
    "needs_info",
    "needs_review",
    "awaiting_return",
    "return_received",
    "completed",
    "rejected",
    "withdrawn",
]
Outcome = Literal[
    "auto_rejected", "needs_info", "needs_review", "approved_pending_return", "auto_approved"
]


# ---------------------------------------------------------------- 공통
class Meta(BaseModel):
    """성공 응답 `{data, meta}`의 meta."""

    total: int | None = None
    tenant_id: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """실패 응답 `{error: {code, message}}`."""

    error: ErrorBody


class Finding(BaseModel):
    code: str
    level: Literal["error", "warning"]
    message: str


class Validation(BaseModel):
    passed: bool
    findings: list[Finding] = Field(default_factory=list)


class Health(BaseModel):
    status: str
    llm_provider: str
    kb_chunks: int
    demo_tools: bool


class Me(BaseModel):
    id: int
    name: str
    email: str
    points: int


class KbChunk(BaseModel):
    id: str
    category: str
    title: str
    section: str
    text: str


class OrderSummary(BaseModel):
    id: int
    order_no: str
    status: str
    status_ko: str
    title: str


# ---------------------------------------------------------------- 클레임
class Reason(BaseModel):
    code: str
    level: Literal["block", "review", "warn", "info"]
    message: str
    rule: str | None = None


class DecisionLine(BaseModel):
    order_item_id: int | str  # 목은 숫자, support는 문자열(shop-api order_item_id)
    title: str
    qty: int
    requested_qty: int
    unit_price: int
    refund_cash: int
    points_back: int


class Deduction(BaseModel):
    code: str
    amount: int
    message: str


class AbuseSignal(BaseModel):
    code: str
    points: int
    message: str


class Abuse(BaseModel):
    score: int
    signals: list[AbuseSignal] = Field(default_factory=list)


class ExchangeShipment(BaseModel):
    tracking: str
    shipped_at: str


class Claim(BaseModel):
    id: int
    order_id: int | str  # 목은 숫자, support는 문자열(shop-api ord_…)
    type: ClaimType
    reason: ClaimReason
    status: ClaimStatus
    created_at: str
    description: str
    claimed_amount: int | None = None
    unsealed: bool = False
    evidence_count: int = 0
    outcome: Outcome | None = None
    resolution: str | None = None
    requires_return: bool = False
    reasons: list[Reason] = Field(default_factory=list)
    lines: list[DecisionLine] = Field(default_factory=list)
    deductions: list[Deduction] = Field(default_factory=list)
    expected_refund: int = 0
    points_back: int = 0
    compensation: int = 0
    payable: int = 0
    tracking: str | None = None
    return_received_at: str | None = None
    inspection: str | None = None
    refund: int = 0
    compensation_paid: int = 0
    points_returned: int = 0
    policy_refs: list[str] = Field(default_factory=list)
    exchange_shipment: ExchangeShipment | None = None
    processed_at: str | None = None
    conversation_id: int | None = None
    order_no: str | None = None
    # 상담원 전용
    customer: str | None = None
    abuse: Abuse | None = None
    decided_by: str | None = None
    agent_note: str | None = None
    hold_reason: str | None = None
    manager_approved: bool | None = None
    inspection_note: str | None = None
    user_id: int | None = None


class AuditEntry(BaseModel):
    actor: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
    at: str


class ReceiptItem(BaseModel):
    id: int | str
    title: str
    qty: int
    paid_amount: int
    points_share: int
    refunded_qty: int


class Receipt(BaseModel):
    order_no: str
    status: str
    items_total: int
    shipping_fee: int
    points_used: int
    paid_amount: int
    paid_at: str
    delivered_at: str | None = None
    expected_delivery_at: str | None = None
    items: list[ReceiptItem] = Field(default_factory=list)


class ClaimDetail(Claim):
    audit: list[AuditEntry] = Field(default_factory=list)
    receipt: Receipt


class ClaimStats(BaseModel):
    total: int
    by_status: dict[str, int]
    rule_hits: dict[str, int]
    agent_reviews: int
    agent_overrides: int
    overridden_rules: dict[str, int]


# ---------------------------------------------------------------- 보상 큐
class Blocker(BaseModel):
    code: str
    message: str


class QueueItemLine(BaseModel):
    title: str
    qty: int


class QueueEntry(BaseModel):
    position: int
    claim_id: int
    order_no: str
    customer: str
    type: ClaimType
    reason: ClaimReason
    received_at: str
    inspection: str | None = None
    amount: int
    compensation: int
    ready: bool
    blockers: list[Blocker] = Field(default_factory=list)
    items: list[QueueItemLine] = Field(default_factory=list)


class QueueView(BaseModel):
    queue: list[QueueEntry]
    awaiting_return: list[Claim]


class QueueRunResult(BaseModel):
    claim_id: int
    result: Literal["paid", "held", "skipped"]
    blockers: list[Blocker] = Field(default_factory=list)
    refund: int | None = None
    compensation: int | None = None


class QueueRun(BaseModel):
    results: list[QueueRunResult]
    paid: int
    held: int
    queue: list[QueueEntry]


# ---------------------------------------------------------------- 문의
class Source(BaseModel):
    id: str
    doc_id: str
    category: str
    title: str
    section: str
    text: str
    score: float
    from_decision: bool | None = None


class Draft(BaseModel):
    style: Literal["empathetic", "concise", "step_by_step"]
    reply: str
    source_ids: list[str] = Field(default_factory=list)
    validation: Validation


class Classification(BaseModel):
    intent_ko: str
    category: str
    sentiment: Literal["calm", "frustrated", "angry"]
    urgency: Urgency
    search_query: str
    pii_masked: list[str] = Field(default_factory=list)


class Analysis(BaseModel):
    classification: Classification | None = None
    sources: list[Source] | None = None
    drafts: list[Draft] | None = None
    timings_ms: dict[str, int] | None = None
    validation: Validation | None = None


class Message(BaseModel):
    id: int
    role: Literal["customer", "agent"]
    text: str
    analysis: Analysis | None = None
    created_at: str


class Summary(BaseModel):
    summary_ko: str
    issue_ko: str
    actions_ko: list[str] = Field(default_factory=list)
    next_action_ko: str
    category: str
    resolved: bool
    escalate: bool
    escalate_reason_ko: str
    tags: list[str] = Field(default_factory=list)


class ConversationListItem(BaseModel):
    id: int
    customer_name: str
    subject: str
    category: str | None = None
    sentiment: str | None = None
    urgency: Urgency | None = None
    status: ConversationStatus
    order_id: int | None = None
    updated_at: str
    preview: str
    awaiting_reply: bool


class Conversation(ConversationListItem):
    messages: list[Message] = Field(default_factory=list)
    summary: Summary | None = None
    order_no: str | None = None
    claims: list[Claim] = Field(default_factory=list)


# ---------------------------------------------------------------- 후기
class AdminReviewFlag(BaseModel):
    code: str
    message: str
    info: bool | None = None


class AdminReview(BaseModel):
    id: int
    product_id: int
    user: str
    rating: int
    title: str
    body: str
    helpful: int
    verified: bool
    created_at: str
    status: str
    product: str
    flags: list[AdminReviewFlag] = Field(default_factory=list)


# ---------------------------------------------------------------- 집계
class Kpi(BaseModel):
    conversations: int
    by_status: dict[str, int]
    agent_replies: int
    draft_adoption_rate: float | None = None
    accepted_as_is: int
    edited: int
    rejected: int
    avg_edit_ratio: float | None = None
    avg_first_draft_ms: int | None = None
    drafts_generated: int
    drafts_blocked_by_guard: int


class DailyPoint(BaseModel):
    date: str
    inquiries: int
    claims: int
    auto: int
    agent: int
    paid: int


class DashboardTotals(BaseModel):
    inquiries: int
    open_waiting: int
    high_waiting: int
    claims: int
    auto_rate: float | None = None
    claims_rejected: int
    needs_review: int
    awaiting_return: int
    queue_waiting: int
    queue_ready: int
    paid_cash: int
    paid_points: int
    agent_reviews: int
    agent_overrides: int
    flagged_reviews: int


class InquiryQueueItem(BaseModel):
    id: int
    customer_name: str
    subject: str
    category: str | None = None
    urgency: str | None = None
    sentiment: str | None = None
    preview: str
    waiting_minutes: int


class Dashboard(BaseModel):
    days: int
    timezone: str
    daily: list[DailyPoint]
    totals: DashboardTotals
    by_category: dict[str, int]
    claim_reasons: dict[str, int]
    guard_findings: dict[str, int]
    queue: list[QueueEntry]
    inquiry_queue: list[InquiryQueueItem]


# ---------------------------------------------------------------- 쓰기 요청
class ConversationCreate(BaseModel):
    user_id: int
    customer_name: str | None = None  # 실제 연결에서 support에 넘긴다. 없으면 "고객 <id>"
    subject: str = Field(min_length=1)
    message: str = Field(min_length=1)
    order_id: int | None = None


class TextBody(BaseModel):
    text: str = Field(min_length=1)


class CandidateDecisionBody(BaseModel):
    """후보 카드 결정. `approve`는 승인(active), `reject`는 폐기(retired)."""

    decision: Literal["approve", "reject"]
    note: str | None = None


class OnboardingStepBody(BaseModel):
    """온보딩 단계 완료 표시. `done: false`면 완료를 취소한다."""

    done: bool = True


class AgentMessageBody(TextBody):
    draft_style: str | None = None
    draft_text: str | None = None


class DraftStyleBody(BaseModel):
    draft_style: str


class StatusBody(BaseModel):
    status: ConversationStatus


class NoteBody(BaseModel):
    note: str = ""


class InspectionBody(BaseModel):
    inspection: str
    note: str | None = None


class QueueProcessBody(BaseModel):
    limit: int | None = Field(default=None, ge=1)
    claim_ids: list[int] | None = None


class ReviewStatusBody(BaseModel):
    status: Literal["visible", "hidden"]
