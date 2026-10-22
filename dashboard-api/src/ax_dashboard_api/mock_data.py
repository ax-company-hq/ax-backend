"""목 모드(`DASHBOARD_MOCK=1`) 데이터. 실제 데이터는 3단계 이후 support·shop-api·memory에서 받는다.

시간은 UTC 고정값이라 테스트가 시간에 기대지 않는다. 집계 숫자는 아래 목록에서 코드로 센다.
"""

from __future__ import annotations

from collections import Counter

from ax_dashboard_api.schemas import (
    AdminReview,
    AdminReviewFlag,
    Analysis,
    AuditEntry,
    Claim,
    ClaimDetail,
    ClaimStats,
    Classification,
    Conversation,
    ConversationListItem,
    DailyPoint,
    Dashboard,
    DashboardTotals,
    DecisionLine,
    Draft,
    Finding,
    Health,
    InquiryQueueItem,
    KbChunk,
    Kpi,
    Me,
    Message,
    OrderSummary,
    QueueEntry,
    QueueItemLine,
    QueueView,
    Reason,
    Receipt,
    ReceiptItem,
    Source,
    Summary,
    Validation,
)

TIMEZONE = "Asia/Seoul"

USERS = [
    Me(id=1, name="김하늘", email="haneul@example.com", points=3200),
    Me(id=2, name="이도윤", email="doyoon@example.com", points=0),
]

KB_CHUNKS = [
    KbChunk(
        id="refund-1",
        category="refund",
        title="환불 기준",
        section="수령 후 7일",
        text="수령 후 7일 이내 단순 변심 환불은 왕복 배송비를 고객이 부담한다.",
    ),
    KbChunk(
        id="refund-2",
        category="refund",
        title="환불 기준",
        section="불량·오배송",
        text="불량·오배송은 기간과 관계없이 판매자 부담으로 환불한다.",
    ),
]

ORDERS = [
    OrderSummary(
        id=101,
        order_no="ORD-2026-0101",
        status="delivered",
        status_ko="배송 완료",
        title="고양이의 하루 외 1권",
    ),
    OrderSummary(
        id=102, order_no="ORD-2026-0102", status="shipped", status_ko="배송 중", title="탁상 달력"
    ),
]
ORDERS_BY_USER = {1: [ORDERS[0]], 2: [ORDERS[1]]}

_PASSED = Validation(passed=True, findings=[])
_WARNED = Validation(
    passed=False,
    findings=[Finding(code="promise_refund", level="error", message="환불을 단정해서 약속함")],
)

_CLAIM_1 = Claim(
    id=1,
    order_id=101,
    order_no="ORD-2026-0101",
    type="return",
    reason="defect",
    status="needs_review",
    created_at="2026-10-20T01:00:00Z",
    description="표지가 찢어져 왔어요",
    outcome="needs_review",
    resolution="환불",
    requires_return=True,
    evidence_count=2,
    reasons=[
        Reason(
            code="DEFECT_EVIDENCE",
            level="review",
            message="사진 증빙 확인 필요",
            rule="defect_evidence",
        )
    ],
    lines=[
        DecisionLine(
            order_item_id=1,
            title="고양이의 하루",
            qty=1,
            requested_qty=1,
            unit_price=15000,
            refund_cash=15000,
            points_back=0,
        )
    ],
    expected_refund=15000,
    payable=15000,
    policy_refs=["refund-2"],
    customer="김하늘",
    user_id=1,
    conversation_id=1,
)
_CLAIM_2 = Claim(
    id=2,
    order_id=101,
    order_no="ORD-2026-0101",
    type="compensation",
    reason="late_delivery",
    status="return_received",
    created_at="2026-10-19T02:00:00Z",
    description="배송이 3일 늦었어요",
    outcome="approved_pending_return",
    resolution="보상",
    requires_return=False,
    compensation=2000,
    payable=2000,
    return_received_at="2026-10-21T03:00:00Z",
    inspection="pass",
    customer="김하늘",
    user_id=1,
)
CLAIMS = [_CLAIM_1, _CLAIM_2]

_RECEIPT = Receipt(
    order_no="ORD-2026-0101",
    status="delivered",
    items_total=30000,
    shipping_fee=3000,
    points_used=0,
    paid_amount=33000,
    paid_at="2026-10-10T00:00:00Z",
    delivered_at="2026-10-14T00:00:00Z",
    items=[
        ReceiptItem(
            id=1, title="고양이의 하루", qty=1, paid_amount=15000, points_share=0, refunded_qty=0
        )
    ],
)
CLAIM_AUDITS = {
    1: [
        AuditEntry(
            actor="rule_engine",
            action="decide",
            detail={"outcome": "needs_review"},
            at="2026-10-20T01:00:05Z",
        )
    ],
    2: [
        AuditEntry(
            actor="rule_engine",
            action="decide",
            detail={"outcome": "approved"},
            at="2026-10-19T02:00:05Z",
        )
    ],
}

QUEUE = [
    QueueEntry(
        position=1,
        claim_id=2,
        order_no="ORD-2026-0101",
        customer="김하늘",
        type="compensation",
        reason="late_delivery",
        received_at="2026-10-21T03:00:00Z",
        inspection="pass",
        amount=0,
        compensation=2000,
        ready=True,
        items=[QueueItemLine(title="고양이의 하루", qty=1)],
    ),
]

CONVERSATIONS = [
    Conversation(
        id=1,
        customer_name="김하늘",
        subject="책 표지가 찢어져 왔어요",
        category="refund",
        sentiment="frustrated",
        urgency="high",
        status="open",
        order_id=101,
        updated_at="2026-10-21T05:00:00Z",
        preview="표지가 찢어져 왔어요. 환불해 주세요.",
        awaiting_reply=True,
        order_no="ORD-2026-0101",
        claims=[_CLAIM_1],
        messages=[
            Message(
                id=1,
                role="customer",
                text="표지가 찢어져 왔어요. 환불해 주세요.",
                created_at="2026-10-21T05:00:00Z",
                analysis=Analysis(
                    classification=Classification(
                        intent_ko="불량 환불 요청",
                        category="refund",
                        sentiment="frustrated",
                        urgency="high",
                        search_query="불량 환불",
                    ),
                    sources=[
                        Source(
                            id="refund-2",
                            doc_id="refund",
                            category="refund",
                            title="환불 기준",
                            section="불량·오배송",
                            text=KB_CHUNKS[1].text,
                            score=0.91,
                        )
                    ],
                    drafts=[
                        Draft(
                            style="empathetic",
                            reply="불편을 드려 죄송합니다. 확인 후 안내드리겠습니다.",
                            source_ids=["refund-2"],
                            validation=_PASSED,
                        ),
                        Draft(
                            style="concise",
                            reply="불량 접수되었습니다. 확인 후 안내드립니다.",
                            source_ids=["refund-2"],
                            validation=_PASSED,
                        ),
                        Draft(
                            style="step_by_step",
                            reply="사진 확인, 회수, 환불 순서로 진행합니다. 환불을 약속합니다.",
                            source_ids=["refund-2"],
                            validation=_WARNED,
                        ),
                    ],
                    timings_ms={"classify": 120, "draft": 800},
                    validation=_PASSED,
                ),
            )
        ],
    ),
    Conversation(
        id=2,
        customer_name="이도윤",
        subject="배송 언제 오나요",
        category="shipping",
        sentiment="calm",
        urgency="low",
        status="resolved",
        order_id=102,
        updated_at="2026-10-20T09:00:00Z",
        preview="내일 도착 예정입니다.",
        awaiting_reply=False,
        order_no="ORD-2026-0102",
        summary=Summary(
            summary_ko="배송 일정 문의",
            issue_ko="배송 지연 여부",
            actions_ko=["배송 조회 안내"],
            next_action_ko="없음",
            category="shipping",
            resolved=True,
            escalate=False,
            escalate_reason_ko="",
            tags=["shipping"],
        ),
        messages=[
            Message(
                id=2, role="customer", text="배송 언제 오나요?", created_at="2026-10-20T08:00:00Z"
            ),
            Message(
                id=3, role="agent", text="내일 도착 예정입니다.", created_at="2026-10-20T09:00:00Z"
            ),
        ],
    ),
]

REVIEWS = [
    AdminReview(
        id=1,
        product_id=11,
        product="고양이의 하루",
        user="kim***",
        rating=1,
        title="최악",
        body="연락주세요 010-0000-0000",
        helpful=0,
        verified=False,
        created_at="2026-10-20T00:00:00Z",
        status="pending",
        flags=[AdminReviewFlag(code="pii", message="전화번호 포함")],
    ),
    AdminReview(
        id=2,
        product_id=12,
        product="탁상 달력",
        user="lee***",
        rating=5,
        title="좋아요",
        body="예뻐요",
        helpful=3,
        verified=True,
        created_at="2026-10-19T00:00:00Z",
        status="visible",
    ),
]

KPI = Kpi(
    conversations=2,
    by_status={"open": 1, "resolved": 1},
    agent_replies=1,
    draft_adoption_rate=0.5,
    accepted_as_is=1,
    edited=0,
    rejected=1,
    avg_edit_ratio=0.1,
    avg_first_draft_ms=920,
    drafts_generated=3,
    drafts_blocked_by_guard=1,
)


def health() -> Health:
    return Health(status="ok", llm_provider="mock", kb_chunks=len(KB_CHUNKS), demo_tools=True)


def list_conversations() -> list[ConversationListItem]:
    return [ConversationListItem.model_validate(c.model_dump()) for c in CONVERSATIONS]


def get_conversation(conversation_id: int) -> Conversation | None:
    return next((c for c in CONVERSATIONS if c.id == conversation_id), None)


def list_claims(status: str | None = None) -> list[Claim]:
    return [c for c in CLAIMS if status is None or c.status == status]


def get_claim(claim_id: int) -> ClaimDetail | None:
    claim = next((c for c in CLAIMS if c.id == claim_id), None)
    if claim is None:
        return None
    return ClaimDetail(**claim.model_dump(), audit=CLAIM_AUDITS.get(claim_id, []), receipt=_RECEIPT)


def claim_stats() -> ClaimStats:
    rule_hits = Counter(r.code for c in CLAIMS for r in c.reasons)
    return ClaimStats(
        total=len(CLAIMS),
        by_status=dict(Counter(c.status for c in CLAIMS)),
        rule_hits=dict(rule_hits),
        agent_reviews=1,
        agent_overrides=0,
        overridden_rules={},
    )


def queue_view() -> QueueView:
    return QueueView(
        queue=QUEUE, awaiting_return=[c for c in CLAIMS if c.status == "awaiting_return"]
    )


def dashboard(days: int) -> Dashboard:
    waiting = [c for c in CONVERSATIONS if c.awaiting_reply]
    return Dashboard(
        days=days,
        timezone=TIMEZONE,
        daily=[DailyPoint(date="2026-10-21", inquiries=1, claims=1, auto=0, agent=1, paid=0)],
        totals=DashboardTotals(
            inquiries=len(CONVERSATIONS),
            open_waiting=len(waiting),
            high_waiting=sum(1 for c in waiting if c.urgency == "high"),
            claims=len(CLAIMS),
            auto_rate=0.0,
            claims_rejected=0,
            needs_review=sum(1 for c in CLAIMS if c.status == "needs_review"),
            awaiting_return=sum(1 for c in CLAIMS if c.status == "awaiting_return"),
            queue_waiting=len(QUEUE),
            queue_ready=sum(1 for q in QUEUE if q.ready),
            paid_cash=0,
            paid_points=0,
            agent_reviews=1,
            agent_overrides=0,
            flagged_reviews=sum(1 for r in REVIEWS if r.flags),
        ),
        by_category=dict(Counter(c.category for c in CONVERSATIONS if c.category)),
        claim_reasons=dict(Counter(c.reason for c in CLAIMS)),
        guard_findings={"promise_refund": 1},
        queue=QUEUE,
        inquiry_queue=[
            InquiryQueueItem(
                id=c.id,
                customer_name=c.customer_name,
                subject=c.subject,
                category=c.category,
                urgency=c.urgency,
                sentiment=c.sentiment,
                preview=c.preview,
                waiting_minutes=30,
            )
            for c in waiting
        ],
    )
