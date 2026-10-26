"""문의가 진행 중인 사건의 번호(`ctx_inc_`)를 이어받는다 (#274, SCN-A 한 사건 번호 기준).

규칙
- 접수 때 한 번만 정한다. 정해지면 문의의 `context_id`가 그 사건 번호가 되고, 이후 후속 메시지·답변·
  상태 변경·보상 요청이 같은 번호로 기록된다 (재무 손실 리포트가 사건 번호로 보상 쿠폰을 센다).
- **주문 사실로만 판정한다.** 고객 문장은 읽지 않는다. 문의의 주문이 결제 서버 문제 코드
  (`PG_UNAVAILABLE`)로 실패한 시도를 갖고 있을 때만, 그 시각을 `occurred_at`으로 총괄
  `find_active_incident(component=payment)`에 묻는다. 증명할 수 없으면 묻지 않는다.
- 조회가 실패하거나 시간이 넘거나 응답이 이상하거나 사건이 없으면 기존 방식
  (`ctx_ui_`·`ctx_shop_`)으로 접수한다. 문의 응대는 멈추지 않는다.
- 남의 주문, 시드 주문, 숫자 주문은 조회하지 않는다. 이미 사건 번호로 온 요청은 다시 묻지 않는다.
- 총괄이 사건에 보관한 점검 공지 번호(`notice_id`, 계약 #356)를 함께 돌려주면 그 공지를
  `get_notice`로 바로 읽는다. 값을 지어내지 않고, 번호 모양이 맞지 않으면 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    FindActiveIncidentInput,
    FindActiveIncidentOutput,
    TaskStatus,
)
from support_agent import buyer_ids, shop_client
from support_agent.payment_handlers import SERVICE_FAULT_CODES

logger = logging.getLogger("ax.support.incident_context")

SendFn = Callable[[str, AgentRequest], Awaitable[AgentResult]]

INCIDENT_PREFIX = "ctx_inc_"
PAYMENT_COMPONENT = "payment"
LOOKUP_TIMEOUT_SECONDS = 3.0  # 사건 조회가 문의 접수를 오래 붙잡지 않게 한다
NOTICE_ID_PATTERN = re.compile(r"^ntc_[0-9A-Za-z_-]{1,40}$")  # 마케팅 공지 번호 모양


@dataclass(frozen=True)
class IncidentRef:
    """총괄이 알려 준 진행 중인 사건. `notice_id`는 사건에 보관된 점검 공지 번호(없으면 None)."""

    context_id: str
    notice_id: str | None = None


def is_incident(context_id: str) -> bool:
    return context_id.startswith(INCIDENT_PREFIX)


def rebind(request: AgentRequest, context_id: str) -> AgentRequest:
    """요청의 사건 번호만 바꾼 복사본을 돌려준다 (나머지 meta는 그대로)."""
    if request.meta.context_id == context_id:
        return request
    return request.model_copy(
        update={"meta": request.meta.model_copy(update={"context_id": context_id})}
    )


def follow_inquiry(request: AgentRequest, conversation_context_id: str) -> AgentRequest:
    """문의가 사건 번호를 이어받았다면 후속 요청도 그 번호로 기록한다.

    호출한 쪽이 이미 사건 번호를 줬으면(총괄 요청) 그대로 둔다.
    """
    if is_incident(conversation_context_id) and not is_incident(request.meta.context_id):
        return rebind(request, conversation_context_id)
    return request


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def provable_occurrence(order: dict[str, Any]) -> tuple[str, datetime] | None:
    """주문 기록으로 증명할 수 있는 (구성요소, 발생 시각). 증명할 수 없으면 None이다.

    결제 서버 문제 코드로 실패한 결제 시도가 있으면 결제 구성요소 문제이고, 그중 가장 이른
    시도 시각을 쓴다. 카드 거절 같은 고객 쪽 실패와 정상 결제는 사건이 아니다.
    """
    times = [
        moment
        for payment in order.get("payments", [])
        if payment.get("status") == "failed" and payment.get("failure_code") in SERVICE_FAULT_CODES
        if (moment := _moment(payment.get("created_at"))) is not None
    ]
    if not times:
        return None
    return PAYMENT_COMPONENT, min(times)


async def lookup(
    request: AgentRequest, send: SendFn, component: str, occurred_at: datetime
) -> IncidentRef | None:
    """총괄에 진행 중인 사건을 묻는다. 어떤 실패든 None이다."""
    query = AgentRequest(
        meta=request.meta.model_copy(update={"idempotency_key": None}),
        skill="find_active_incident",
        input=FindActiveIncidentInput(component=component, occurred_at=occurred_at).model_dump(
            mode="json"
        ),
    )
    try:
        reply = await asyncio.wait_for(send("orchestrator", query), LOOKUP_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("find_active_incident 호출 실패", exc_info=True)
        return None
    if reply.status != TaskStatus.COMPLETED:
        return None
    try:
        found = FindActiveIncidentOutput.model_validate(reply.output)
    except ValidationError:
        return None
    if found.found and found.context_id and is_incident(found.context_id):
        notice_id = found.notice_id if _is_notice_id(found.notice_id) else None
        return IncidentRef(found.context_id, notice_id)
    return None


def _is_notice_id(value: Any) -> bool:
    return isinstance(value, str) and NOTICE_ID_PATTERN.fullmatch(value) is not None


async def resolve_for_order(
    request: AgentRequest, order_id: Any, user_id: int, send: SendFn
) -> IncidentRef | None:
    """문의의 주문으로 진행 중인 사건을 찾는다. 찾지 못하면 None (기존 방식으로 접수)."""
    if is_incident(request.meta.context_id):
        return None
    if not isinstance(order_id, str) or not order_id.startswith("ord_"):
        return None
    if shop_client.order_source() == "seed":
        return None
    try:
        order = await shop_client.fetch_order_facts(order_id)
    except Exception:
        logger.warning("사건 조회용 주문 읽기 실패", exc_info=True)
        return None
    if order is None or order.get("buyer_id") != buyer_ids.from_number(user_id):
        return None  # 남의 주문으로 사건에 묶지 않는다
    occurrence = provable_occurrence(order)
    if occurrence is None:
        return None
    component, occurred_at = occurrence
    return await lookup(request, send, component, occurred_at)
