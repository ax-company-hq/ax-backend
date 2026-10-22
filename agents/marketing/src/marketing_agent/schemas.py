"""marketing skill 입출력 모델. Agent Card의 `input_schema`·`output_schema`가 가리키는 곳이다.

이벤트 data(`NoticePublishedData`, `CouponIssuedData`, `Promise`)는 계약(`ax_contracts`)을 쓴다.
여기에는 skill 입출력만 둔다. 금액은 정수 원, 시간은 UTC다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas import Promise
from ax_contracts.schemas.common import AxModel, UtcDatetime

ChannelCode = Literal["banner", "email", "sns"]
NoticeKind = Literal["incident_opened", "incident_resolved", "product_created"]


class NoticeFacts(AxModel):
    """공지에 쓸 수 있는 사실. 원인·판단 문장은 받지 않는다."""

    component: Literal["api", "db", "payment"] | None = None
    opened_at: UtcDatetime | None = None
    resolved_at: UtcDatetime | None = None
    product_id: str | None = None
    category_code: str | None = None


class DraftNoticeInput(AxModel):
    kind: NoticeKind
    facts: NoticeFacts
    channel_code: ChannelCode = "banner"
    promises: list[Promise] = Field(default_factory=list)
    valid_from: UtcDatetime | None = None
    valid_to: UtcDatetime | None = None


class NoticeDraft(AxModel):
    draft_id: str
    kind: NoticeKind
    channel_code: ChannelCode
    public_text: str
    facts: NoticeFacts
    promises: list[Promise]
    valid_from: UtcDatetime | None = None
    valid_to: UtcDatetime | None = None


class PublishNoticeInput(AxModel):
    draft_id: str


class PublishedNotice(AxModel):
    notice_id: str
    channel_code: ChannelCode
    status: Literal["published"] = "published"


class GetNoticeInput(AxModel):
    notice_id: str


class NoticeView(AxModel):
    """`get_notice` 응답. 찾지 못하면 `found=False`이고 나머지는 비운다(확인할 수 없음)."""

    found: bool
    notice_id: str | None = None
    status: Literal["published", "ended"] | None = None
    channel_code: ChannelCode | None = None
    public_text: str | None = None
    facts: NoticeFacts | None = None
    promises: list[Promise] = Field(default_factory=list)
    valid_from: UtcDatetime | None = None
    valid_to: UtcDatetime | None = None
    unknown_reason: str | None = None


class IssueCouponInput(AxModel):
    purpose_code: Literal["campaign", "compensation"]
    buyer_id: str
    count: int = Field(gt=0)
    unit_value: int = Field(gt=0)
    campaign_id: str | None = None
    claimed_total: int | None = (
        None  # 요청 쪽 계산값. 코드가 다시 계산해 다르면 safety가 C로 보낸다
    )


class IssuedCoupon(AxModel):
    coupon_id: str
    purpose_code: Literal["campaign", "compensation"]
    count: int
    total_value: int
