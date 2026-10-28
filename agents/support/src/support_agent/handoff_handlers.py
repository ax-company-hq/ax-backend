"""`prepare_handoff`(읽기): 문의를 사람에게 넘길 때 필요한 인계 패키지를 코드가 모아 준다.

- 이관 사유, 이미 한 조치, 대기 시간, 반복 횟수는 코드가 센다(`handoff_rules.py`).
  LLM은 사람이 읽을 요약 한 문단(`summary_ko`)만 맡고, 실패하면 고정 문장이다.
  그 문장이 틀려도 코드 필드는 그대로다.
- 아무것도 바꾸지 않는다: 상태를 `escalated`로 바꾸지 않고 권고만 한다 (바꾸는 일은 기존 쓰기 경로).
- 고객 원문, 이름, 연락처를 출력에 싣지 않는다. 가명 구매자 ID와 사실 필드만 담는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ax_contracts.schemas import AgentRequest, AgentResult, Evidence
from support_agent import (
    buyer_ids,
    customer_signals,
    drafting,
    guard,
    handoff_rules,
    payment_handlers,
    privacy_rules,
    shop_client,
)
from support_agent import claim_handlers as claim_h
from support_agent import handlers as h
from support_agent.handoff_schemas import (
    ActionTaken,
    HandoffPackage,
    HandoffReason,
    OpenItem,
    PrepareHandoffInput,
)
from support_agent.schemas import Conversation

NOTES = [
    "참고 의견이다. 이관 여부는 사람이 정하고, 이 skill은 상태를 바꾸지 않는다",
    "이미 한 조치에 환불이 끝난 클레임이 있으면 같은 환불을 다시 하지 않는다",
]
UNDECIDED_CLAIM = ("needs_review", "needs_info")
HUMAN_DECISIONS = ("approve", "reject")


def now() -> datetime:
    """테스트에서 바꿀 수 있다."""
    return datetime.now(UTC)


def _customer_messages(conversation: Conversation) -> list[Any]:
    return [m for m in conversation.messages if m.role == "customer"]


def _claims_of(conversation: Conversation) -> list[Any]:
    return sorted(
        (c for c in claim_h.CLAIMS.list() if c.conversation_id == conversation.id),
        key=lambda c: c.id,
    )


def _repeat_count(conversation: Conversation, at: datetime) -> int:
    """같은 구매자, 같은 분류의 최근 문의 수. 분류를 모르면 이 문의 하나로 센다."""
    if conversation.user_id is None or not conversation.category:
        return 1
    since = at - handoff_rules.REPEAT_WINDOW
    count = 0
    for other in h.STORE.all():
        if other.user_id != conversation.user_id or other.category != conversation.category:
            continue
        first = next((m for m in other.messages if m.role == "customer"), None)
        if first is not None and since < customer_signals.parse_time(first.created_at) <= at:
            count += 1
    return max(count, 1)


def _blocked_messages(conversation: Conversation) -> int:
    """답변 가드가 초안을 하나라도 막은 고객 메시지 수."""
    blocked = 0
    for message in _customer_messages(conversation):
        drafts = message.analysis.drafts if message.analysis and message.analysis.drafts else []
        if any(not d.validation.passed for d in drafts):
            blocked += 1
    return blocked


def _no_evidence(conversation: Conversation) -> bool:
    """마지막 고객 메시지의 근거 검색이 비어 있고 분류가 없거나 일반(general)인가."""
    messages = _customer_messages(conversation)
    analysis = messages[-1].analysis if messages else None
    if analysis is None or analysis.sources is None:
        return False  # 분석 기록이 없으면 모른다
    category = analysis.classification.category if analysis.classification else None
    return not analysis.sources and category in (None, "general")


def _reason(code: str, **facts: int | str | bool) -> HandoffReason:
    severity, target = handoff_rules.REASONS[code]
    return HandoffReason(code=code, severity=severity, target=target, facts=facts)  # type: ignore[arg-type]


async def _payment_reason(
    conversation: Conversation, notes: list[str]
) -> tuple[HandoffReason | None, ActionTaken | None]:
    """결제 진단(shop-api 읽기)이 사람 확인을 가리키는가. 못 읽으면 패키지를 막지 않고 알린다."""
    if not conversation.order_id or shop_client.order_source() == "seed":
        notes.append("결제 기록을 확인할 수 없어 결제 검토 여부는 판단하지 않았다")
        return None, None
    try:
        data = await shop_client.fetch_order_facts(str(conversation.order_id))
    except shop_client.ShopError:
        notes.append("결제 기록을 읽지 못해 결제 검토 여부는 판단하지 않았다")
        return None, None
    buyer = (
        buyer_ids.from_number(conversation.user_id) if conversation.user_id is not None else None
    )
    # 문의를 낸 구매자를 모르면 어느 주문도 그 사람 것이라고 볼 수 없다.
    # 남의 주문 기록도 쓰지 않는다.
    if buyer is None or data is None or data.get("buyer_id") != buyer:
        notes.append("문의의 주문을 확인할 수 없어 결제 검토 여부는 판단하지 않았다")
        return None, None
    diagnosis = payment_handlers.diagnose(data)
    action = ActionTaken(
        kind="payment_diagnosis", ref_id=f"order:{data['order_id']}", status=diagnosis.situation
    )
    if diagnosis.next_action in ("review_refund", "handoff_review"):
        reason = _reason(
            "payment_review", situation=diagnosis.situation, next_action=diagnosis.next_action
        )
        return reason, action
    return None, action


def _actions_and_open(
    claims: list[Any], conversation: Conversation
) -> tuple[list[ActionTaken], int, list[OpenItem]]:
    actions: list[ActionTaken] = []
    open_items: list[OpenItem] = []
    refunded = 0
    for claim in claims:
        decided_by_agent = any(a.action in HUMAN_DECISIONS for a in claim.audit)
        paid = claim.status == "completed" and claim.expected_refund > 0
        refunded += claim.expected_refund if paid else 0
        actions.append(
            ActionTaken(
                kind="claim",
                ref_id=f"claim:{claim.id}",
                status=claim.status,
                actor="agent" if decided_by_agent else "system",
                amount=claim.expected_refund if paid else None,
            )
        )
        if claim.status in UNDECIDED_CLAIM:
            open_items.append(OpenItem(code="claim_review", ref_id=f"claim:{claim.id}"))
        elif claim.status == "awaiting_return":
            open_items.append(OpenItem(code="claim_return", ref_id=f"claim:{claim.id}"))
        elif claim.status == "return_received":
            open_items.append(OpenItem(code="claim_payout", ref_id=f"claim:{claim.id}"))
    for link in h.CHANGES.for_inquiry(conversation.id):
        actions.append(
            ActionTaken(
                kind="change_request",
                ref_id=link.request_id or link.context_id,
                status=link.status,
            )
        )
        if link.status == "requested":
            open_items.append(OpenItem(code="change_pending", ref_id=link.request_id))
    if conversation.awaiting_reply:
        open_items.insert(0, OpenItem(code="awaiting_reply"))
    return actions, refunded, open_items


def _collect_reasons(
    conversation: Conversation, claims: list[Any], at: datetime
) -> tuple[list[HandoffReason], int]:
    """코드 규칙으로 이관 사유를 모은다. 반환: (사유들, 같은 분류 문의 횟수)."""
    texts = " ".join(m.text for m in _customer_messages(conversation))
    reasons: list[HandoffReason] = []
    if handoff_rules.is_explicit_human_request(texts):
        reasons.append(_reason("explicit_human_request"))
    if conversation.category == privacy_rules.CATEGORY or privacy_rules.is_deletion_request(texts):
        reasons.append(_reason("privacy_request"))
    repeat = _repeat_count(conversation, at)
    own = len(_customer_messages(conversation))
    if repeat >= handoff_rules.REPEAT_COUNT or (
        own >= handoff_rules.REPEAT_COUNT and conversation.status != "resolved"
    ):
        reasons.append(_reason("repeated_topic", inquiries=repeat, customer_messages=own))
    undecided = [c for c in claims if c.status in UNDECIDED_CLAIM]
    if undecided:
        reasons.append(_reason("claim_needs_review", claims=len(undecided)))
    if conversation.user_id is not None:
        buyer = buyer_ids.from_number(conversation.user_id)
        groups = customer_signals.group_by_buyer(h.STORE.all(), claim_h.CLAIMS.list())
        convs, buyer_claims = groups.get(buyer, ([], []))
        signals = customer_signals.compute(buyer, convs, buyer_claims, at)
        if signals.level == "high":
            reasons.append(_reason("high_risk_customer", score=signals.score))
    if conversation.context_id.startswith("ctx_inc_"):
        reasons.append(_reason("incident_related"))
    blocked = _blocked_messages(conversation)
    if blocked >= handoff_rules.BLOCKED_MESSAGES:
        reasons.append(_reason("reply_blocked", blocked_messages=blocked))
    if _no_evidence(conversation):
        reasons.append(_reason("no_evidence"))
    return reasons, repeat


async def build_package(
    conversation: Conversation, at: datetime, *, include_payment: bool = False
) -> HandoffPackage:
    """인계 패키지를 만든다 (읽기만). 순서: 사유, 한 조치, 요약."""
    notes = list(NOTES)
    claims = _claims_of(conversation)
    reasons, repeat = _collect_reasons(conversation, claims, at)
    payment_action: ActionTaken | None = None
    if include_payment:
        payment_reason, payment_action = await _payment_reason(conversation, notes)
        if payment_reason is not None:
            reasons.append(payment_reason)

    actions, refunded, open_items = _actions_and_open(claims, conversation)
    if payment_action is not None:
        actions.append(payment_action)

    top = max((handoff_rules.severity_rank(r.severity) for r in reasons), default=-1)
    recommendation = "none" if top < 0 else handoff_rules.SEVERITY_ORDER[top]
    targets: list[str] = []
    for reason in sorted(reasons, key=lambda r: -handoff_rules.severity_rank(r.severity)):
        if reason.target not in targets:
            targets.append(reason.target)

    waiting: int | None = None
    messages = _customer_messages(conversation)
    if conversation.awaiting_reply and messages:
        last = customer_signals.parse_time(messages[-1].created_at)
        waiting = max(int((at - last).total_seconds() // 60), 0)

    summary, fallback = await drafting.summarize(conversation)
    buyer = (
        buyer_ids.from_number(conversation.user_id) if conversation.user_id is not None else None
    )
    return HandoffPackage(
        inquiry_id=conversation.id,
        buyer_id=buyer,
        category=conversation.category,
        status=conversation.status,
        reasons=reasons,
        recommendation=recommendation,  # type: ignore[arg-type]
        handoff_targets=targets,
        actions_taken=actions,
        already_refunded=refunded > 0,
        refunded_amount=refunded,
        open_items=open_items,
        waiting_minutes=waiting,
        repeat_count=repeat,
        summary_ko=guard.mask_pii(summary.summary_ko)[0],
        summary_source="template" if fallback else "llm",
        notes=notes,
    )


async def prepare_handoff(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, PrepareHandoffInput)
    if isinstance(parsed, AgentResult):
        return parsed
    conversation = h.STORE.get(parsed.inquiry_id)
    if conversation is None:
        return h._not_found(request, parsed.inquiry_id)
    package = await build_package(conversation, now(), include_payment=parsed.include_payment)
    evidence = Evidence(
        queries=["inquiry_store", "claim_store", "change_request_store"],
        ref_ids=[f"inquiry:{conversation.id}"] + [a.ref_id for a in package.actions_taken],
        data_summary=(
            f"문의 {conversation.id}: 권고 {package.recommendation}, "
            f"사유 {[r.code for r in package.reasons]}"
        ),
    )
    return h._done(
        request,
        package.model_dump(mode="json"),
        outcome=package.recommendation,
        fallback=package.summary_source == "template",
        evidence=evidence,
    )


HANDOFF_HANDLERS = {"prepare_handoff": prepare_handoff}
