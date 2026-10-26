"""고객 이탈·재문의 위험 신호 skill의 입출력. 읽기이고 고객에게 아무것도 보내지 않는다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel

RiskLevel = Literal["low", "medium", "high"]
RecommendedAction = Literal["none", "proactive_check", "personal_followup"]


class CustomerSignalsInput(AxModel):
    buyer_id: str  # 구매자 ID (buy_…). 옛 usr_<번호>는 buy_로 바꿔 읽는다


class ListAtRiskInput(AxModel):
    min_level: Literal["medium", "high"] = "medium"
    limit: int = Field(default=20, ge=1, le=50)


class SignalCounts(AxModel):
    """코드가 문의·클레임 기록에서 센 건수. 이름이나 원문은 담지 않는다."""

    inquiries_30d: int = 0
    open_inquiries: int = 0
    unanswered_over_24h: int = 0
    escalated_inquiries: int = 0
    angry_messages_30d: int = 0
    frustrated_messages_30d: int = 0
    claims_90d: int = 0
    rejected_claims_90d: int = 0


class SignalReason(AxModel):
    code: str
    count: int
    points: int


class CustomerSignals(AxModel):
    """`score`는 이유별 점수의 합이고, 단계는 점수 기준표로 정한다 (`customer_signals.py`).

    위험 단계는 사실에서 코드가 낸 값이며 고객을 평가하는 말이 아니다. 쓰는 사람이 확인한다.
    """

    buyer_id: str
    level: RiskLevel
    score: int
    reasons: list[SignalReason] = Field(default_factory=list)
    counts: SignalCounts
    last_contact_at: str | None = None
    recommended_action: RecommendedAction
    claim_pattern_review: bool = False  # 클레임이 잦아 담당자가 패턴을 볼 만하다 (이탈과 별개)


class AtRiskCustomers(AxModel):
    evaluated_at: str
    min_level: str
    total: int  # 조건에 맞는 전체 수 (limit으로 자르기 전)
    items: list[CustomerSignals]
