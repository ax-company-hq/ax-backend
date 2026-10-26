"""고객지원이 발행하는 이벤트. 발행은 agent-template `events.publish`로만 한다.

`support.inquiry_received`: 문의 접수와 후속 고객 메시지마다 한 번씩 발행한다 (SHOP-09).
- `data`는 사실 필드만 담는다 (`SupportInquiryReceivedData`). 판단 문장은 넣지 않는다.
- 고객 원문은 봉투의 `untrusted`에만 담고, 전화번호·이메일은 가린 뒤 넣는다.
- 같은 문의·같은 메시지 수는 같은 `event_id`라서 재전송해도 한 번만 전달된다.

`support.inquiry_resolved`: 문의를 닫을 때(해결·이관) 문의당 한 번 발행한다 (#327).
- `data`는 사실 필드만 담는다 (`SupportInquiryResolvedData`). 고객 원문은 싣지 않는다.
- `resolution`: 이관이면 `escalated`, 붙은 클레임이 판정됐으면 `claim_decided`, 아니면 `answered`.
  클레임은 `conversation_id`로 잇고(가장 나중 것), `claim_decision`은 상담원이 승인·거절했을
  때만 채운다(시스템 자동 판정은 비운다). `refund_amount`는 환불까지 끝난(`completed`)
  클레임만 싣는다.
  `coupon_id`는 문의와 마케팅 쿠폰을 잇는 정보가 아직 없어 비운다.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from ax_agent_template.events import publish
from ax_agent_template.logging import log_event
from ax_contracts.schemas import AgentRequest, EventEnvelope, EventType
from ax_contracts.schemas.event import SupportInquiryReceivedData, SupportInquiryResolvedData
from support_agent import guard
from support_agent.claim_schemas import Claim
from support_agent.schemas import Conversation

SOURCE = "support"
logger = logging.getLogger("ax.support.events")


def inquiry_received_key(context_id: str, inquiry_id: int, message_count: int) -> str:
    return f"{context_id}:{EventType.SUPPORT_INQUIRY_RECEIVED.value}:{inquiry_id}:{message_count}"


def build_inquiry_received(request: AgentRequest, conversation: Conversation) -> EventEnvelope:
    """저장이 끝난 문의에서 이벤트 봉투를 만든다. 마지막 메시지가 방금 받은 고객 메시지다."""
    customer = [m for m in conversation.messages if m.role == "customer"]
    last = customer[-1]
    classification = last.analysis.classification if last.analysis else None
    key = inquiry_received_key(request.meta.context_id, conversation.id, len(customer))
    order_id = conversation.order_id
    data = SupportInquiryReceivedData(
        inquiry_id=conversation.id,
        # 계약의 `order_id`는 `ord_` 문자열이다. 시드 주문("101")처럼 그 형식이 아닌 ID는 지어서
        # 바꾸지 않고 비워 둔다.
        order_id=order_id if order_id and order_id.startswith("ord_") else None,
        category=classification.category if classification else None,
        urgency=classification.urgency if classification else None,
        message_count=len(customer),
    )
    return EventEnvelope(
        event_id=f"evt_{hashlib.sha256(key.encode()).hexdigest()[:12]}",
        type=EventType.SUPPORT_INQUIRY_RECEIVED,
        occurred_at=datetime.now(UTC),
        tenant_id=request.meta.tenant_id,
        context_id=request.meta.context_id,
        source=SOURCE,
        idempotency_key=key,
        data=data.model_dump(mode="json"),
        untrusted={
            "subject": guard.mask_pii(conversation.subject)[0],
            "message": guard.mask_pii(last.text)[0],
        },
    )


def inquiry_resolved_key(context_id: str, inquiry_id: int) -> str:
    return f"{context_id}:{EventType.SUPPORT_INQUIRY_RESOLVED.value}:{inquiry_id}"


# 상담원 확인 전(판정 대기)인 클레임 상태. 이 밖이면 판정이 끝난 것이다
_UNDECIDED = ("needs_info", "needs_review")


def build_inquiry_resolved(
    request: AgentRequest, conversation: Conversation, claim: Claim | None = None
) -> EventEnvelope:
    """닫힌 문의에서 이벤트 봉투를 만든다. 문의당 한 번이라 키에 문의 번호만 쓴다."""
    key = inquiry_resolved_key(request.meta.context_id, conversation.id)
    escalated = conversation.status == "escalated"
    order_id = conversation.order_id
    resolution = "escalated" if escalated else "answered"
    claim_decision = None
    refund_amount = None
    if claim is not None:
        claim_decision = next(
            (a.action for a in reversed(claim.audit) if a.action in ("approve", "reject")), None
        )
        if not escalated and claim.status not in _UNDECIDED:
            resolution = "claim_decided"
        if claim.status == "completed" and claim.expected_refund > 0:
            refund_amount = claim.expected_refund
    data = SupportInquiryResolvedData(
        inquiry_id=conversation.id,
        order_id=order_id if order_id and order_id.startswith("ord_") else None,
        status="escalated" if escalated else "resolved",
        resolution=resolution,  # type: ignore[arg-type]
        claim_id=claim.id if claim is not None else None,
        claim_decision=claim_decision,  # type: ignore[arg-type]
        refund_amount=refund_amount,
    )
    return EventEnvelope(
        event_id=f"evt_{hashlib.sha256(key.encode()).hexdigest()[:12]}",
        type=EventType.SUPPORT_INQUIRY_RESOLVED,
        occurred_at=datetime.now(UTC),
        tenant_id=request.meta.tenant_id,
        context_id=request.meta.context_id,
        source=SOURCE,
        idempotency_key=key,
        data=data.model_dump(mode="json"),
    )


async def _publish(request: AgentRequest, envelope: EventEnvelope) -> None:
    """발행 실패는 요청을 막지 않는다 (사실은 이미 저장됐다)."""
    try:
        await publish(envelope)
    except NotImplementedError:  # Redis 연동 전(TODO)
        log_event(
            request.meta.context_id,
            request.meta.tenant_id,
            "event_publish_skipped",
            {"type": envelope.type.value},
        )
    except Exception:
        logger.warning("%s 발행 실패", envelope.type.value, exc_info=True)


async def emit_inquiry_received(request: AgentRequest, conversation: Conversation) -> None:
    await _publish(request, build_inquiry_received(request, conversation))


async def emit_inquiry_resolved(
    request: AgentRequest, conversation: Conversation, claim: Claim | None = None
) -> None:
    """문의가 해결·이관으로 닫힐 때 한 번 발행한다 (`open`으로 되돌리면 발행하지 않는다)."""
    if conversation.status in ("resolved", "escalated"):
        await _publish(request, build_inquiry_resolved(request, conversation, claim))
