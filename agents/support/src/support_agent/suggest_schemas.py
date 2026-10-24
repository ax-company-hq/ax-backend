"""보상 후보 제안 skill의 입출력. 제안은 읽기이고 쿠폰은 승인 뒤에 요청한다."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel
from support_agent.schemas import RequestCompensationInput


class SuggestCompensationInput(AxModel):
    inquiry_id: int


class CompensationFacts(AxModel):
    """판정에 쓴 주문 사실 (shop-api 값 그대로, 없으면 None)."""

    order_id: str
    promised_delivery_at: datetime | None = None
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    days_late: int | None = None  # 코드가 센 값. 약속일을 모르면 None


class CompensationSuggestion(AxModel):
    """`eligible`이면 `request`를 사람이 승인해 `request_compensation`에 그대로 보낼 수 있다.

    `message`는 코드가 만든 사실 문장이다 (고객 원문이나 LLM 문장이 아니다).
    """

    eligible: bool
    reason_code: Literal["late_delivery"] | None = None
    message: str
    unit_value: int = 0
    count: int = 0
    total_value: int = 0
    facts: CompensationFacts | None = None
    request: RequestCompensationInput | None = None
    notes: list[str] = Field(default_factory=list)
