"""영수증 분석과 환불·교환 요청 분석 skill의 입출력. 모두 읽기라 상태를 바꾸지 않는다."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel
from support_agent.claim_schemas import ClaimReason, ClaimType, CreateClaimInput

MAX_IMAGES = 4
MAX_IMAGE_BASE64 = 6_000_000  # 사진 한 장 (약 4.5MB). 크기를 넘으면 읽지 않는다


class EvidenceImageIn(AxModel):
    """고객이 올린 사진. 외부 입력이라 `untrusted.images`로만 받는다."""

    ref: str = Field(max_length=200)
    media_type: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"
    data_base64: str | None = Field(default=None, max_length=MAX_IMAGE_BASE64)


class AnalyzeReceiptInput(AxModel):
    """영수증 글은 `untrusted.receipt_text`, 사진은 `untrusted.images`로 받는다.

    분석하는 주문은 문의에 붙은 주문이다. 다른 주문을 지정하면 거부한다.
    """

    inquiry_id: int
    order_id: str | None = None


class ReceiptItemOut(AxModel):
    title: str
    qty: int
    amount: int


class ReceiptFactsOut(AxModel):
    """영수증에서 코드가 뽑은 사실. 못 찾은 값은 None이고 원문은 담지 않는다."""

    total: int | None = None
    paid_on: date | None = None
    order_no: str | None = None
    shipping_fee: int | None = None
    items: list[ReceiptItemOut] = Field(default_factory=list)
    ambiguous_total: bool = False


class OrderFactsOut(AxModel):
    """대조에 쓴 주문 사실 (shop-api 값)."""

    order_id: str
    cash_paid: int  # 실제 결제한 현금 (배송비 포함, 포인트 제외)
    gross_total: int  # 포인트를 쓰기 전 총액
    shipping_fee: int
    paid_on: date
    item_titles: list[str]


class ReceiptCheckOut(AxModel):
    verdict: Literal["consistent", "mismatch", "insufficient"]
    checked: list[str] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)  # RECEIPT_… 코드
    matched_basis: Literal["cash", "gross"] | None = None
    unmatched_receipt_items: list[str] = Field(default_factory=list)
    missing_from_receipt: list[str] = Field(default_factory=list)


class AnalyzeReceiptOutput(AxModel):
    """`images_status`: `none`(사진 없음) / `read`(읽음) / `unavailable`(이미지 분석 미연결)
    / `failed`(읽다가 실패) / `skipped`(개수·크기 제한을 넘음).
    """

    facts: ReceiptFactsOut
    order: OrderFactsOut
    check: ReceiptCheckOut
    images_status: Literal["none", "read", "unavailable", "failed", "skipped"] = "none"
    pii_masked: list[str] = Field(default_factory=list)  # 가린 개인정보 종류 (값은 담지 않는다)
    notes: list[str] = Field(default_factory=list)


class AnalyzeClaimRequestInput(AxModel):
    """문의의 마지막 고객 메시지를 분석한다 (원문은 저장소에서 읽고 개인정보를 가려 쓴다)."""

    inquiry_id: int


RequestKind = Literal["refund", "exchange", "cancel", "event_inquiry", "other"]


class LLMClaimRequest(AxModel):
    """LLM이 분류만 한다. 금액·기간·가능 여부는 코드가 정한다."""

    request_kind: RequestKind
    reason: ClaimReason | None = None
    items_mentioned: list[str] = Field(default_factory=list, max_length=10)
    event_keywords: list[str] = Field(default_factory=list, max_length=10)


class PreviewLine(AxModel):
    order_item_id: str
    title: str
    qty: int
    refund_cash: int


class PreviewReason(AxModel):
    code: str
    level: str
    message: str


class ClaimPreview(AxModel):
    """접수했다면 어떻게 판정될지 규칙으로 미리 계산한 값 (접수하지 않는다)."""

    outcome: str
    resolution: str
    requires_return: bool
    expected_refund: int
    shipping_refund: int
    reasons: list[PreviewReason] = Field(default_factory=list)
    lines: list[PreviewLine] = Field(default_factory=list)


class PolicySource(AxModel):
    id: str
    title: str
    section: str


class AnalyzeClaimRequestOutput(AxModel):
    request_kind: RequestKind
    reason: ClaimReason | None = None
    claim_type: ClaimType | None = None  # 환불은 배송 전이면 cancel, 배송 후면 return
    items: list[str] = Field(default_factory=list)  # 고객이 말한 주문 품목 (주문 품목과 맞춘 것)
    unmatched_items: list[str] = Field(default_factory=list)  # 주문에서 찾지 못한 품목 이름
    event_related: bool = False
    event_keywords: list[str] = Field(default_factory=list)
    preview: ClaimPreview | None = None
    suggested_claim: CreateClaimInput | None = None  # 담당자가 확인한 뒤 create_claim에 보낼 입력
    policy_sources: list[PolicySource] = Field(default_factory=list)
    source: Literal["llm", "rules"] = "llm"  # 분류를 LLM이 했는지, 규칙으로 대신했는지
    notes: list[str] = Field(default_factory=list)
