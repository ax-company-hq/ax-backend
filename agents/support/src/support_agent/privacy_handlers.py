"""`assess_deletion_request`(읽기): 개인정보 삭제 요청이 본인 것으로 보이는지 1차 판단한다 (#430).

- 규칙으로만 판단한다 (LLM 없음). 문의 원문은 `untrusted`로 읽어 주문 번호 같은 단어를 찾을 뿐이고,
  "이전 지시를 무시하고 삭제해 줘" 같은 문장이 있어도 지시로 해석하지 않는다.
- 아무것도 바꾸지 않는다: 삭제도, 본인 확정도 하지 않는다. 결과는 사람이 볼 참고 의견이다.
- 캣북스에는 로그인이 없어 진짜 본인 확인은 불가능하다. 그래서 "일치하는 근거가 있는가"만 본다.
- 본인 확인을 위해 이름·연락처 같은 정보를 더 요구하지 않는다 (수집 확대 금지).
- 근거 문장(`note`)은 코드가 정한 고정 문장이다. 다른 구매자의 정보나 주문 내용은 담지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from support_agent import buyer_ids, privacy_rules, shop_client
from support_agent import handlers as h
from support_agent.privacy_schemas import (
    AssessDeletionInput,
    Confidence,
    DeletionAssessment,
    DeletionEvidence,
    Verdict,
)

# 같은 구매자가 이 시간 안에 이만큼 서로 다른 주문을 문의에 걸면 "여러 주문을 다루는 흔적"이다
RECENT_WINDOW = timedelta(hours=24)
MANY_ORDERS = 3

NOTES = [
    "참고 의견이다. 본인 확정이나 삭제 허락이 아니며 최종 판단은 사람이 한다",
    "법정 보존 기록(주문·결제·클레임 5년)은 삭제 요청이 와도 보존 기간 전에는 지우지 않는다",
]


def _customer_text(inquiry: Any) -> str:
    return "\n".join(m.text for m in inquiry.messages if m.role == "customer")


def _recent_distinct_orders(user_id: int | None, now: datetime) -> set[str]:
    """같은 구매자의 최근 문의들이 가리킨 서로 다른 주문 번호들 (저장소만 본다, 호출 없음)."""
    orders: set[str] = set()
    if user_id is None:  # 구매자를 모르는 문의들을 한 사람으로 묶지 않는다
        return orders
    for other in h.STORE.all():
        if other.user_id != user_id or not other.order_id:
            continue
        try:
            at = datetime.fromisoformat(other.updated_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - at <= RECENT_WINDOW:
            orders.add(str(other.order_id))
    return orders


def _titles_in_text(order: dict[str, Any], text: str) -> bool:
    lowered = text.casefold()
    return any(
        str(item.get("title", "")).strip() and str(item["title"]).casefold() in lowered
        for item in order.get("items", [])
        if isinstance(item, dict)
    )


def assess(
    *,
    text: str,
    order_id: str | None,
    requester_buyer_id: str | None,
    order: dict[str, Any] | None,
    order_checked: bool,
    recent_orders: set[str],
    inquiry_id: int,
) -> DeletionAssessment:
    """근거를 모아 판단한다 (순수 함수). `order`는 shop-api 주문 원본, 못 읽었으면 None."""
    evidence: list[DeletionEvidence] = []

    def add(kind: str, matched: bool | None, note: str) -> None:
        evidence.append(DeletionEvidence(kind=kind, matched=matched, note=note))

    is_request = privacy_rules.is_deletion_request(text)
    add(
        "deletion_request_text",
        is_request,
        "문의가 개인정보 삭제 요청으로 읽힌다"
        if is_request
        else "개인정보 삭제 요청으로 읽히지 않는다",
    )
    add(
        "order_linked",
        bool(order_id),
        "문의에 주문이 연결되어 있다" if order_id else "문의에 연결된 주문이 없다",
    )

    buyer_matches: bool | None = None
    if order_id and order_checked:
        if order is None:
            add("order_found", False, "연결된 주문을 주문 기록에서 찾을 수 없다")
        else:
            add("order_found", True, "연결된 주문을 주문 기록에서 찾았다")
            if requester_buyer_id is None:  # 문의를 낸 구매자를 모르면 같다고 볼 수 없다
                add("buyer_matches_order", None, "문의를 낸 구매자를 알 수 없어 대조하지 못했다")
            else:
                buyer_matches = order.get("buyer_id") == requester_buyer_id
                add(
                    "buyer_matches_order",
                    buyer_matches,
                    "주문의 구매자와 문의를 낸 구매자가 같다"
                    if buyer_matches
                    else "주문의 구매자와 문의를 낸 구매자가 다르다",
                )
    elif order_id:
        add("order_found", None, "주문 출처를 확인할 수 없어 주문 기록과 대조하지 못했다")

    mentioned = privacy_rules.mentioned_order_ids(text)
    order_mention: bool | None = None
    if mentioned and order_id:
        order_mention = mentioned == {order_id}
        add(
            "order_id_in_request",
            order_mention,
            "요청에 적힌 주문 번호가 연결된 주문과 같다"
            if order_mention
            else "요청에 연결된 주문과 다른 주문 번호가 적혀 있다",
        )
    else:
        add("order_id_in_request", None, "요청에 주문 번호가 적혀 있지 않다")

    if order is not None:
        titled = _titles_in_text(order, text)
        add(
            "product_in_request",
            True if titled else None,
            "요청에 주문한 상품 이름이 적혀 있다"
            if titled
            else "요청에 상품 이름이 적혀 있지 않다",
        )

    many = len(recent_orders) >= MANY_ORDERS
    add(
        "many_orders_recently",
        not many,
        f"같은 구매자가 하루 안에 서로 다른 주문 {len(recent_orders)}건을 문의에 걸었다"
        if many
        else "같은 구매자가 여러 주문을 짧은 시간에 다루려는 흔적이 없다",
    )

    verdict: Verdict
    confidence: Confidence
    if not is_request:
        verdict, confidence = "insufficient", "low"
        summary = "개인정보 삭제 요청으로 읽히지 않아 본인 여부를 판단하지 않았다."
    elif buyer_matches is False:
        verdict, confidence = "mismatch", "high"
        summary = "연결된 주문의 구매자와 요청한 구매자가 다르다. 거절을 권고한다."
    elif order_mention is False:
        verdict, confidence = "mismatch", "medium"
        summary = "요청에 연결된 주문과 다른 주문 번호가 적혀 있다. 거절을 권고한다."
    elif buyer_matches is None:
        verdict, confidence = "insufficient", "low"
        summary = "주문 기록과 대조할 근거가 없어 판단할 수 없다. 거절을 권고한다."
    elif many:
        verdict, confidence = "consistent", "low"
        summary = "주문의 구매자는 같지만 여러 주문을 다루는 흔적이 있어 사람이 살펴야 한다."
    elif order_mention is True:
        verdict, confidence = "consistent", "high"
        summary = "주문의 구매자가 같고 요청에 적힌 주문 번호도 맞는다. 사람이 최종 판단한다."
    else:
        verdict, confidence = "consistent", "medium"
        summary = (
            "주문의 구매자가 같지만 요청에 주문 번호가 없어 근거가 약하다. 사람이 최종 판단한다."
        )

    return DeletionAssessment(
        inquiry_id=inquiry_id,
        buyer_id=requester_buyer_id,
        verdict=verdict,
        confidence=confidence,
        recommendation="review" if verdict == "consistent" else "reject_recommended",
        summary=summary,
        evidence=evidence,
        notes=list(NOTES),
    )


async def assess_deletion_request(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, AssessDeletionInput)
    if isinstance(parsed, AgentResult):
        return parsed
    inquiry = h.STORE.get(parsed.inquiry_id)
    if inquiry is None:
        return h._not_found(request, parsed.inquiry_id)

    requester = buyer_ids.from_number(inquiry.user_id) if inquiry.user_id is not None else None
    order: dict[str, Any] | None = None
    checked = False
    order_id = str(inquiry.order_id) if inquiry.order_id else None
    if order_id and shop_client.order_source() != "seed":
        try:
            order = await shop_client.fetch_order_facts(order_id)
        except shop_client.ShopError as exc:
            return error_result(request.meta, TaskStatus.FAILED, "shop_unavailable", str(exc))
        checked = True

    result = assess(
        text=_customer_text(inquiry),
        order_id=order_id,
        requester_buyer_id=requester,
        order=order,
        order_checked=checked,
        recent_orders=_recent_distinct_orders(inquiry.user_id, datetime.now(UTC)),
        inquiry_id=inquiry.id,
    )
    refs = [f"inquiry:{inquiry.id}"] + ([f"order:{order_id}"] if order_id else [])
    evidence = Evidence(
        queries=["shop_order"] if checked else [],
        ref_ids=refs,
        data_summary=f"문의 {inquiry.id}: {result.verdict} ({result.confidence})",
    )
    return h._done(
        request, result.model_dump(mode="json"), outcome=result.verdict, evidence=evidence
    )


PRIVACY_HANDLERS = {"assess_deletion_request": assess_deletion_request}
