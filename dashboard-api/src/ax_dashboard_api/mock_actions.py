"""목 모드 쓰기 동작. 프로세스 메모리의 `mock_data` 목록을 바꾼다.

실제 연결(3단계 이후)에서는 승인·지급 계열이 support 에이전트의 `execute()`(A2A)로 넘어간다.
여기서는 그 결과를 흉내만 내며 감사 기록(`CLAIM_AUDITS`)에 남긴다. 시간은 UTC ISO8601이다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ax_dashboard_api import mock_data
from ax_dashboard_api.envelope import ApiError
from ax_dashboard_api.schemas import (
    AdminReview,
    AuditEntry,
    Blocker,
    Claim,
    ClaimDetail,
    Conversation,
    ConversationListItem,
    Finding,
    Message,
    QueueRun,
    QueueRunResult,
    Summary,
    Validation,
)

ACTOR = "agent_console"
_PROMISES = ("무조건 환불", "100% 환불", "반드시 환불")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conversation(conversation_id: int) -> Conversation:
    found = mock_data.get_conversation(conversation_id)
    if found is None:
        raise ApiError(404, "NOT_FOUND", f"문의 {conversation_id}를 찾을 수 없다")
    return found


def _claim(claim_id: int) -> Claim:
    found = next((c for c in mock_data.CLAIMS if c.id == claim_id), None)
    if found is None:
        raise ApiError(404, "NOT_FOUND", f"클레임 {claim_id}를 찾을 수 없다")
    return found


def _audit(claim_id: int, action: str, **detail: Any) -> None:
    mock_data.CLAIM_AUDITS.setdefault(claim_id, []).append(
        AuditEntry(actor=ACTOR, action=action, detail=detail, at=_now())
    )


def _detail(claim_id: int) -> ClaimDetail:
    detail = mock_data.get_claim(claim_id)
    assert detail is not None
    return detail


def _touch(conv: Conversation, text: str, awaiting: bool) -> None:
    conv.updated_at = _now()
    conv.preview = text[:80]
    conv.awaiting_reply = awaiting


def _next_message_id(conv: Conversation) -> int:
    return max((m.id for m in conv.messages), default=0) + 1


# ---------------------------------------------------------------- 문의
def create_conversation(
    user_id: int, subject: str, message: str, order_id: int | str | None
) -> Conversation:
    user = next((u for u in mock_data.USERS if u.id == user_id), None)
    if user is None:
        raise ApiError(404, "NOT_FOUND", f"고객 {user_id}를 찾을 수 없다")
    now = _now()
    order = next((o for o in mock_data.ORDERS if o.id == order_id), None) if order_id else None
    conv = Conversation(
        id=max((c.id for c in mock_data.CONVERSATIONS), default=0) + 1,
        customer_name=user.name,
        subject=subject,
        status="open",
        order_id=order_id,
        order_no=order.order_no if order else None,
        updated_at=now,
        preview=message[:80],
        awaiting_reply=True,
        messages=[Message(id=1, role="customer", text=message, created_at=now)],
    )
    mock_data.CONVERSATIONS.append(conv)
    return conv


def add_customer_message(conversation_id: int, text: str) -> Conversation:
    conv = _conversation(conversation_id)
    conv.messages.append(
        Message(id=_next_message_id(conv), role="customer", text=text, created_at=_now())
    )
    conv.status = "open"
    _touch(conv, text, awaiting=True)
    return conv


def check_reply(conversation_id: int, text: str) -> Validation:
    """답변 가드 흉내: 환불 단정 문구만 막는다. 실제 가드는 support의 reply guard다."""
    _conversation(conversation_id)
    if not any(p in text for p in _PROMISES):
        return Validation(passed=True, findings=[])
    return Validation(
        passed=False,
        findings=[Finding(code="promise_refund", level="error", message="환불을 단정해서 약속함")],
    )


def add_agent_message(conversation_id: int, text: str) -> Conversation:
    conv = _conversation(conversation_id)
    verdict = check_reply(conversation_id, text)
    if not verdict.passed:
        raise ApiError(422, "REPLY_BLOCKED", verdict.findings[0].message)
    conv.messages.append(
        Message(id=_next_message_id(conv), role="agent", text=text, created_at=_now())
    )
    _touch(conv, text, awaiting=False)
    return conv


def reject_draft(conversation_id: int) -> None:
    _conversation(conversation_id)


def summarize(conversation_id: int) -> Summary:
    conv = _conversation(conversation_id)
    customer = [m.text for m in conv.messages if m.role == "customer"]
    agent = [m.text for m in conv.messages if m.role == "agent"]
    summary = Summary(
        summary_ko=f"{conv.customer_name} 고객 문의 {len(conv.messages)}건 대화",
        issue_ko=customer[0] if customer else conv.subject,
        actions_ko=agent[-1:],
        next_action_ko="답변 대기" if conv.awaiting_reply else "추가 문의 대기",
        category=conv.category or "general",
        resolved=conv.status == "resolved",
        escalate=conv.status == "escalated",
        escalate_reason_ko="상담원 판단으로 이관" if conv.status == "escalated" else "",
    )
    conv.summary = summary
    return summary


def set_status(conversation_id: int, status: str) -> ConversationListItem:
    conv = _conversation(conversation_id)
    conv.status = status  # type: ignore[assignment]  # 요청 모델의 Literal로 이미 검증됨
    conv.updated_at = _now()
    return ConversationListItem.model_validate(conv.model_dump())


# ---------------------------------------------------------------- 클레임
def approve_claim(claim_id: int, note: str) -> ClaimDetail:
    claim = _claim(claim_id)
    if claim.status != "needs_review":
        raise ApiError(409, "INVALID_STATE", f"{claim.status} 상태에서는 승인할 수 없다")
    claim.status = "awaiting_return" if claim.requires_return else "completed"
    claim.decided_by = ACTOR
    claim.agent_note = note
    claim.manager_approved = True
    _audit(claim_id, "approve", note=note)
    return _detail(claim_id)


def reject_claim(claim_id: int, note: str) -> ClaimDetail:
    claim = _claim(claim_id)
    if claim.status not in ("needs_review", "needs_info"):
        raise ApiError(409, "INVALID_STATE", f"{claim.status} 상태에서는 거절할 수 없다")
    claim.status = "rejected"
    claim.decided_by = ACTOR
    claim.agent_note = note
    claim.manager_approved = False
    _audit(claim_id, "reject", note=note)
    return _detail(claim_id)


def receive_claim(claim_id: int, inspection: str, note: str | None) -> ClaimDetail:
    claim = _claim(claim_id)
    if claim.status != "awaiting_return":
        raise ApiError(409, "INVALID_STATE", f"{claim.status} 상태에서는 입고 처리할 수 없다")
    claim.status = "return_received"
    claim.return_received_at = _now()
    claim.inspection = inspection
    claim.inspection_note = note
    _audit(claim_id, "receive", inspection=inspection, note=note)
    return _detail(claim_id)


def inspect_claim(claim_id: int, inspection: str, note: str | None) -> ClaimDetail:
    claim = _claim(claim_id)
    if claim.status != "return_received":
        raise ApiError(409, "INVALID_STATE", f"{claim.status} 상태에서는 검수할 수 없다")
    claim.inspection = inspection
    claim.inspection_note = note
    _audit(claim_id, "inspection", inspection=inspection, note=note)
    return _detail(claim_id)


# ---------------------------------------------------------------- 보상 큐
def process_queue(limit: int | None, claim_ids: list[int] | None) -> QueueRun:
    """ready 항목은 지급, 아닌 항목은 보류로 돌려준다. 실제로는 support `execute()`를 거친다."""
    results: list[QueueRunResult] = []
    paid = held = 0
    for entry in list(mock_data.QUEUE):
        if claim_ids is not None and entry.claim_id not in claim_ids:
            continue
        if limit is not None and len(results) >= limit:
            break
        if not entry.ready:
            held += 1
            results.append(
                QueueRunResult(
                    claim_id=entry.claim_id,
                    result="held",
                    blockers=[Blocker(code=b.code, message=b.message) for b in entry.blockers],
                )
            )
            continue
        claim = _claim(entry.claim_id)
        claim.status = "completed"
        claim.processed_at = _now()
        claim.compensation_paid = entry.compensation
        claim.refund = entry.amount
        _audit(entry.claim_id, "pay", amount=entry.amount, compensation=entry.compensation)
        mock_data.QUEUE.remove(entry)
        paid += 1
        results.append(
            QueueRunResult(
                claim_id=entry.claim_id,
                result="paid",
                refund=entry.amount,
                compensation=entry.compensation,
            )
        )
    for position, entry in enumerate(mock_data.QUEUE, start=1):
        entry.position = position
    return QueueRun(results=results, paid=paid, held=held, queue=list(mock_data.QUEUE))


# ---------------------------------------------------------------- 후기
def set_review_status(review_id: int, status: str) -> AdminReview:
    review = next((r for r in mock_data.REVIEWS if r.id == review_id), None)
    if review is None:
        raise ApiError(404, "NOT_FOUND", f"후기 {review_id}를 찾을 수 없다")
    review.status = status
    return review
