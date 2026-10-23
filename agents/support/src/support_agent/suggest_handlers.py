"""`suggest_compensation`(읽기): 문의의 주문 사실로 보상 후보를 제안한다.

고객이 쓴 문장은 읽지 않는다. 주문(shop-api)의 약속 배송일·배송 완료 시각으로 코드가 지연 일수를
세고, 정책표(`compensation.py`)로 단가를 정한다. 아무것도 바꾸지 않고 쿠폰도 내지 않는다.
담당자가 승인하면 응답의 `request`를 그대로 `request_compensation`에 보낸다 (마케팅 `issue_coupon`은
B 등급이라 재무 검증을 거치고 기준 금액 이상은 안전 레이어가 보류한다).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from support_agent import buyer_ids, compensation, shop_client
from support_agent import handlers as h
from support_agent.schemas import RequestCompensationInput
from support_agent.suggest_schemas import (
    CompensationFacts,
    CompensationSuggestion,
    SuggestCompensationInput,
)


def now() -> datetime:
    """테스트에서 바꿀 수 있다."""
    return datetime.now(UTC)


def _not_eligible(message: str, facts: CompensationFacts | None = None) -> dict:
    return CompensationSuggestion(eligible=False, message=message, facts=facts).model_dump(
        mode="json"
    )


async def suggest_compensation(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, SuggestCompensationInput)
    if isinstance(parsed, AgentResult):
        return parsed
    inquiry = h.STORE.get(parsed.inquiry_id)
    if inquiry is None:
        return h._not_found(request, parsed.inquiry_id)
    if not inquiry.order_id:
        return h._done(
            request, _not_eligible("문의에 주문이 없어 판정할 수 없다"), outcome="no_order"
        )
    try:
        order = await shop_client.load_order(inquiry.order_id)
    except shop_client.ShopError as exc:
        return error_result(request.meta, TaskStatus.FAILED, "shop_unavailable", str(exc))
    buyer_id = buyer_ids.from_number(inquiry.user_id)
    if order is None or order.buyer_id != buyer_id:  # 남의 주문으로 보상을 만들지 않는다
        return h._not_found(request, parsed.inquiry_id)

    late = compensation.days_late(order.promised_delivery_at, order.delivered_at, now())
    facts = CompensationFacts(
        order_id=order.id,
        promised_delivery_at=order.promised_delivery_at,
        shipped_at=order.shipped_at,
        delivered_at=order.delivered_at,
        days_late=late,
    )
    evidence = Evidence(
        queries=["shop_order"],
        ref_ids=[f"inquiry:{inquiry.id}", f"order:{order.id}"],
        data_summary=f"주문 {order.id}: 지연 {late if late is not None else '확인할 수 없음'}일",
    )
    if late is None:
        return h._done(
            request,
            _not_eligible("약속한 배송일이 없는 주문이라 지연을 확인할 수 없다", facts),
            outcome="not_eligible",
            evidence=evidence,
        )
    if late < compensation.LATE_MIN_DAYS:
        return h._done(
            request,
            _not_eligible(f"약속한 배송일을 넘기지 않았다 (지연 {late}일)", facts),
            outcome="not_eligible",
            evidence=evidence,
        )
    decision = compensation.decide("late_delivery", 1)
    status = "배송 완료" if order.delivered_at else "아직 배송 전"
    suggestion = CompensationSuggestion(
        eligible=True,
        reason_code="late_delivery",
        message=f"약속한 배송일보다 {late}일 늦었다 ({status}). 지연 보상 쿠폰 후보",
        unit_value=decision.unit_value,
        count=decision.count,
        total_value=decision.total_value,
        facts=facts,
        request=RequestCompensationInput(
            inquiry_id=inquiry.id,
            buyer_id=buyer_id,
            reason_code="late_delivery",
            count=decision.count,
        ),
        notes=["담당자가 승인하면 `request`를 request_compensation에 보낸다"],
    )
    return h._done(
        request, suggestion.model_dump(mode="json"), outcome="suggested", evidence=evidence
    )


SUGGEST_HANDLERS = {"suggest_compensation": suggest_compensation}
