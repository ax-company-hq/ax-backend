"""고객 이탈·재문의 위험 신호. 프레임워크에 의존하지 않는 순수 코드다 (LLM 없음).

데이터분석팀 업무 정리표의 "이탈 예측"과 "고객 행동 분석"을 고객지원이 가진 데이터
(문의·클레임)로 좁혀 적용한 것이다. 구매 이력이 필요한 RFM·LTV·코호트는 데이터분석팀(data)의
몫이라 여기서 하지 않는다.

점수 기준표(아래 상수)로 이유별 점수를 더하고, 합이 `HIGH_AT` 이상이면 high, `MEDIUM_AT` 이상이면
medium이다. 고객 이름과 문의 원문은 읽지도 담지도 않는다 (구매자 가명 ID와 건수만).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from support_agent import buyer_ids
from support_agent.claim_schemas import Claim
from support_agent.insights import parse_time
from support_agent.schemas import Conversation
from support_agent.signal_schemas import (
    CustomerSignals,
    RecommendedAction,
    RiskLevel,
    SignalCounts,
    SignalReason,
)

INQUIRY_WINDOW_DAYS = 30
CLAIM_WINDOW_DAYS = 90
UNANSWERED_HOURS = 24

# 이유별 점수 (코드 = 이유 이름)
POINTS_UNANSWERED = 3  # 하루 넘게 답을 못 받은 문의가 있다
POINTS_ANGRY = 2  # 화난 메시지 1건당 (최대 2건까지 센다)
POINTS_FRUSTRATED = 1  # 답답해하는 메시지 1건당 (최대 2건까지 센다)
SENTIMENT_CAP = 2
POINTS_REPEAT = 2  # 30일 안에 문의가 3건 이상이다
REPEAT_AT = 3
POINTS_ESCALATED = 1  # 담당자 이관된 문의가 있다
POINTS_REJECTED = 2  # 90일 안에 거절된 클레임이 있다
POINTS_FREQUENT_CLAIMS = 2  # 90일 안에 클레임이 2건 이상이다
FREQUENT_CLAIMS_AT = 2
CLAIM_PATTERN_AT = 3  # 클레임이 3건 이상이면 이탈과 별개로 담당자가 패턴을 본다

MEDIUM_AT = 3
HIGH_AT = 6

ACTION_BY_LEVEL: dict[RiskLevel, RecommendedAction] = {
    "low": "none",
    "medium": "proactive_check",
    "high": "personal_followup",
}
LEVEL_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _buyer_of_inquiry(conversation: Conversation) -> str | None:
    return buyer_ids.from_number(conversation.user_id) if conversation.user_id is not None else None


def level_for(score: int) -> RiskLevel:
    if score >= HIGH_AT:
        return "high"
    if score >= MEDIUM_AT:
        return "medium"
    return "low"


def compute(
    buyer_id: str,
    conversations: list[Conversation],
    claims: list[Claim],
    now: datetime,
) -> CustomerSignals:
    """한 구매자의 신호. `conversations`·`claims`는 그 구매자 것만 넘긴다."""
    since_inquiry = now - timedelta(days=INQUIRY_WINDOW_DAYS)
    since_claim = now - timedelta(days=CLAIM_WINDOW_DAYS)
    counts = SignalCounts()
    last_contact: datetime | None = None

    for conversation in conversations:
        updated = parse_time(conversation.updated_at)
        in_window = False
        for message in conversation.messages:
            if message.role != "customer":
                continue
            at = parse_time(message.created_at)
            last_contact = max(last_contact, at) if last_contact else at
            if not (since_inquiry < at <= now):
                continue
            in_window = True
            sentiment = (
                message.analysis.classification.sentiment
                if message.analysis and message.analysis.classification
                else None
            )
            if sentiment == "angry":
                counts.angry_messages_30d += 1
            elif sentiment == "frustrated":
                counts.frustrated_messages_30d += 1
        if in_window:
            counts.inquiries_30d += 1
        if conversation.status == "open":
            counts.open_inquiries += 1
            if conversation.awaiting_reply and now - updated >= timedelta(hours=UNANSWERED_HOURS):
                counts.unanswered_over_24h += 1
        if conversation.status == "escalated":
            counts.escalated_inquiries += 1

    for claim in claims:
        if since_claim < parse_time(claim.created_at) <= now:
            counts.claims_90d += 1
            if claim.status == "rejected":
                counts.rejected_claims_90d += 1

    reasons: list[SignalReason] = []

    def add(code: str, count: int, points: int) -> None:
        if count > 0 and points > 0:
            reasons.append(SignalReason(code=code, count=count, points=points))

    add("unanswered_over_24h", counts.unanswered_over_24h, POINTS_UNANSWERED)
    angry = min(counts.angry_messages_30d, SENTIMENT_CAP)
    add("angry_messages", counts.angry_messages_30d, angry * POINTS_ANGRY)
    frustrated = min(counts.frustrated_messages_30d, SENTIMENT_CAP)
    add("frustrated_messages", counts.frustrated_messages_30d, frustrated * POINTS_FRUSTRATED)
    if counts.inquiries_30d >= REPEAT_AT:
        add("repeat_contact", counts.inquiries_30d, POINTS_REPEAT)
    add("escalated_inquiry", counts.escalated_inquiries, POINTS_ESCALATED)
    add("rejected_claim", counts.rejected_claims_90d, POINTS_REJECTED)
    if counts.claims_90d >= FREQUENT_CLAIMS_AT:
        add("frequent_claims", counts.claims_90d, POINTS_FREQUENT_CLAIMS)

    score = sum(r.points for r in reasons)
    level = level_for(score)
    return CustomerSignals(
        buyer_id=buyer_id,
        level=level,
        score=score,
        reasons=reasons,
        counts=counts,
        last_contact_at=last_contact.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if last_contact
        else None,
        recommended_action=ACTION_BY_LEVEL[level],
        claim_pattern_review=counts.claims_90d >= CLAIM_PATTERN_AT,
    )


def group_by_buyer(
    conversations: list[Conversation], claims: list[Claim]
) -> dict[str, tuple[list[Conversation], list[Claim]]]:
    groups: dict[str, tuple[list[Conversation], list[Claim]]] = {}
    for conversation in conversations:
        buyer = _buyer_of_inquiry(conversation)
        if buyer:
            groups.setdefault(buyer, ([], []))[0].append(conversation)
    for claim in claims:
        buyer = buyer_ids.normalize(claim.buyer_id)
        groups.setdefault(buyer, ([], []))[1].append(claim)
    return groups


def at_risk(
    conversations: list[Conversation],
    claims: list[Claim],
    now: datetime,
    *,
    min_level: str = "medium",
) -> list[CustomerSignals]:
    """기준 단계 이상인 구매자, 점수가 높은 순 (같으면 구매자 ID순)."""
    floor = LEVEL_ORDER[min_level]
    found = [
        compute(buyer, convs, cls, now)
        for buyer, (convs, cls) in group_by_buyer(conversations, claims).items()
    ]
    kept = [s for s in found if LEVEL_ORDER[s.level] >= floor]
    kept.sort(key=lambda s: (-s.score, s.buyer_id))
    return kept
