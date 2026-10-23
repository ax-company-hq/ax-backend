"""고객지원이 발행하는 이벤트. 발행은 agent-template `events.publish`로만 한다.

`support.inquiry_received`: 문의 접수와 후속 고객 메시지마다 한 번씩 발행한다 (SHOP-09).
- `data`는 사실 필드만 담는다 (`SupportInquiryReceivedData`). 판단 문장은 넣지 않는다.
- 고객 원문은 봉투의 `untrusted`에만 담고, 전화번호·이메일은 가린 뒤 넣는다.
- 같은 문의·같은 메시지 수는 같은 `event_id`라서 재전송해도 한 번만 전달된다.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from ax_agent_template.events import publish
from ax_agent_template.logging import log_event
from ax_contracts.schemas import AgentRequest, EventEnvelope, EventType
from ax_contracts.schemas.event import SupportInquiryReceivedData
from support_agent import guard
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


async def emit_inquiry_received(request: AgentRequest, conversation: Conversation) -> None:
    """이벤트를 발행한다. 발행 실패는 문의 접수를 막지 않는다 (사실은 이미 저장됐다)."""
    envelope = build_inquiry_received(request, conversation)
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
        logger.warning("support.inquiry_received 발행 실패", exc_info=True)
