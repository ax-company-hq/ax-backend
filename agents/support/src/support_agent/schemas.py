"""support skill의 입력·출력 모델과 LLM 응답 스키마.

필드 이름은 dashboard-web 화면이 받는 형태(`types.ts`)와 맞춘다. 고객 문의 원문(제목·본문)은
`AgentRequest.untrusted`로만 들어오므로 입력 모델에는 없다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel
from support_agent.claim_schemas import EntityId

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
    language: str = "ko"  # 고객 메시지의 언어 (`languages.detect`, 코드가 판별: ko·en·ja·zh·other)
    keywords: list[str] = Field(default_factory=list)  # 핵심 단어 최대 5개 (개인정보 제거)


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
    language: str = "ko"  # `reply`를 쓴 언어 (고객 언어, 가드가 검사하지 못하는 언어는 en)
    reply_ko: str | None = None  # 한국어가 아닐 때 상담원이 읽을 한국어 번역


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
    order_id: EntityId | None = None
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
    keywords: list[str] = Field(default_factory=list)


class StyleReply(AxModel):
    style: DraftStyle
    reply: str
    reply_ko: str | None = None


class LLMDrafts(AxModel):
    replies: list[StyleReply]


# ---------------------------------------------------------------- skill 입력
class ListInquiriesInput(AxModel):
    status: InquiryStatus | None = None
    user_id: int | None = None  # 이 고객의 문의만 (고객 화면을 대신 부르는 shop-api는 필수)


class GetInquiryInput(AxModel):
    inquiry_id: int
    user_id: int | None = None  # 이 고객의 문의일 때만 (shop-api는 필수, 남의 문의는 not_found)


class CreateInquiryInput(AxModel):
    """제목·본문은 `untrusted`의 `subject`·`message`로 받는다."""

    user_id: int
    customer_name: str
    order_id: EntityId | None = None
    order_no: str | None = None
    notice_id: str | None = None  # 문의가 가리키는 공지. 있으면 마케팅 get_notice로 사실을 읽는다
    # 공지 번호를 모를 때 찾을 구성요소. 이게 있거나 사건(ctx_inc_) 요청이면 게시 중인 공지를 찾는다
    notice_component: Literal["api", "db", "payment"] | None = None


class AddCustomerMessageInput(AxModel):
    """본문은 `untrusted`의 `message`로 받는다."""

    inquiry_id: int
    notice_id: str | None = None
    notice_component: Literal["api", "db", "payment"] | None = None  # CreateInquiryInput과 같다
    user_id: int | None = None  # shop-api는 필수: 이 고객의 문의에만 메시지를 덧붙일 수 있다


class CheckReplyInput(AxModel):
    """검사할 답변은 `untrusted`의 `text`로 받는다."""

    inquiry_id: int | None = None


class SendReplyInput(AxModel):
    """답변은 `untrusted`의 `text`로 받는다. 초안을 썼다면 `draft_style`·`draft_text`도 적는다."""

    inquiry_id: int
    draft_style: DraftStyle | None = None
    draft_text: str | None = None


RejectReason = Literal["wrong_info", "missing_policy", "tone", "too_long", "other"]


class RejectDraftInput(AxModel):
    inquiry_id: int
    draft_style: DraftStyle
    # 거절한 이유. 문서가 없어서(missing_policy)인지 말투 때문인지 가르는 지식 공백 신호다 (선택)
    reason_code: RejectReason | None = None


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


class RequestCompensationOutput(AxModel):
    """`request_compensation` 결과. 마케팅 회신은 상태·outcome·출력을 그대로 담는다."""

    eligible: bool
    reason: str | None = None  # 대상이 아닐 때 이유
    sequence: int | None = None
    count: int | None = None
    unit_value: int | None = None
    total_value: int | None = None
    marketing_status: str | None = None
    marketing_outcome: str | None = None
    marketing_output: dict[str, Any] = Field(default_factory=dict)
