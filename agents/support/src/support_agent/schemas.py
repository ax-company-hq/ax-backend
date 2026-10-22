"""support skill의 입력·출력 모델과 LLM 응답 스키마.

필드 이름은 dashboard-web 화면이 받는 형태(`types.ts`)와 맞춘다. 고객 문의 원문(제목·본문)은
`AgentRequest.untrusted`로만 들어오므로 입력 모델에는 없다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel

Urgency = Literal["low", "medium", "high"]
Sentiment = Literal["calm", "frustrated", "angry"]
InquiryStatus = Literal["open", "resolved", "escalated"]
DraftStyle = Literal["empathetic", "concise", "step_by_step"]
STYLES: tuple[DraftStyle, ...] = ("empathetic", "concise", "step_by_step")


class Finding(AxModel):
    code: str
    level: Literal["error", "warning"]
    message: str


class Validation(AxModel):
    """답변 가드 검사 결과. `error`가 하나라도 있으면 통과하지 못한다."""

    passed: bool
    findings: list[Finding] = Field(default_factory=list)


class Classification(AxModel):
    intent_ko: str
    category: str
    sentiment: Sentiment
    urgency: Urgency
    search_query: str
    pii_masked: list[str] = Field(default_factory=list)


class Source(AxModel):
    id: str
    doc_id: str
    category: str
    title: str
    section: str
    text: str
    score: float
    from_decision: bool | None = None


class Draft(AxModel):
    style: DraftStyle
    reply: str
    source_ids: list[str] = Field(default_factory=list)
    validation: Validation


class Analysis(AxModel):
    classification: Classification | None = None
    sources: list[Source] | None = None
    drafts: list[Draft] | None = None
    timings_ms: dict[str, int] | None = None
    validation: Validation | None = None


class Message(AxModel):
    id: int
    role: Literal["customer", "agent"]
    text: str
    analysis: Analysis | None = None
    created_at: str


class Summary(AxModel):
    summary_ko: str
    issue_ko: str
    actions_ko: list[str] = Field(default_factory=list)
    next_action_ko: str
    category: str
    resolved: bool
    escalate: bool
    escalate_reason_ko: str
    tags: list[str] = Field(default_factory=list)


class ConversationListItem(AxModel):
    id: int
    customer_name: str
    subject: str
    category: str | None = None
    sentiment: str | None = None
    urgency: Urgency | None = None
    status: InquiryStatus
    order_id: int | None = None
    updated_at: str
    preview: str
    awaiting_reply: bool


class Conversation(ConversationListItem):
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str
    user_id: int | None = None
    messages: list[Message] = Field(default_factory=list)
    summary: Summary | None = None
    order_no: str | None = None
    claims: list[dict[str, Any]] = Field(default_factory=list)  # 5단계에서 채운다


# ---------------------------------------------------------------- LLM 응답 스키마
class LLMClassification(AxModel):
    intent_ko: str
    category: str
    sentiment: Sentiment
    urgency: Urgency
    search_query: str


class StyleReply(AxModel):
    style: DraftStyle
    reply: str


class LLMDrafts(AxModel):
    replies: list[StyleReply]


# ---------------------------------------------------------------- skill 입력
class ListInquiriesInput(AxModel):
    status: InquiryStatus | None = None


class GetInquiryInput(AxModel):
    inquiry_id: int


class CreateInquiryInput(AxModel):
    """제목·본문은 `untrusted`의 `subject`·`message`로 받는다."""

    user_id: int
    customer_name: str
    order_id: int | None = None
    order_no: str | None = None


class AddCustomerMessageInput(AxModel):
    """본문은 `untrusted`의 `message`로 받는다."""

    inquiry_id: int


class CheckReplyInput(AxModel):
    """검사할 답변은 `untrusted`의 `text`로 받는다."""

    inquiry_id: int | None = None


class SendReplyInput(AxModel):
    """답변은 `untrusted`의 `text`로 받는다. 초안을 썼다면 `draft_style`·`draft_text`도 적는다."""

    inquiry_id: int
    draft_style: DraftStyle | None = None
    draft_text: str | None = None


class RejectDraftInput(AxModel):
    inquiry_id: int
    draft_style: DraftStyle


class SummarizeInquiryInput(AxModel):
    inquiry_id: int


class SetInquiryStatusInput(AxModel):
    inquiry_id: int
    status: InquiryStatus


class RequestCompensationInput(AxModel):
    """보상 요청. 사유 코드(`late_delivery`, `damaged` 등)로 단가를 코드가 판정한다.

    `buyer_id`는 보상 쿠폰을 받을 구매자다. 문의 원문은 받지 않는다.
    """

    inquiry_id: int
    buyer_id: str
    reason_code: str
    count: int = 1
