"""영수증 분석(`analyze_receipt`)과 환불·교환 요청 분석(`analyze_claim_request`). 둘 다 읽기다.

- 영수증: 글에서 금액·날짜·주문번호·품목을 코드가 뽑아 주문과 대조한다 (`receipts.py`). 사진은
  이미지 분석기를 꽂았을 때만 읽고, 아니면 읽지 못했다고 알린다. 원문은 저장하지 않는다.
- 요청 분석: LLM은 요청 종류·사유·품목·이벤트 단어를 **분류만** 한다. 주문 품목 맞추기,
  클레임 유형,
  환불 범위(`claim_rules.decide`)는 코드가 계산한다. 접수는 하지 않고, 담당자가 확인한 뒤 보낼
  `create_claim` 입력(`suggested_claim`)을 돌려준다.
- 고객 글은 개인정보를 가린 뒤 데이터 블록으로만 LLM에 넣는다. 지시로 해석하지 않는다.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from pydantic import ValidationError

from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from support_agent import claim_handlers, claim_rules, drafting, guard, kb, receipts, shop_client
from support_agent import handlers as h
from support_agent.analysis_schemas import (
    MAX_IMAGE_BASE64,
    MAX_IMAGES,
    AnalyzeClaimRequestInput,
    AnalyzeClaimRequestOutput,
    AnalyzeReceiptInput,
    AnalyzeReceiptOutput,
    ClaimPreview,
    EvidenceImageIn,
    LLMClaimRequest,
    OrderFactsOut,
    PolicySource,
    PreviewLine,
    PreviewReason,
    ReceiptCheckOut,
    ReceiptFactsOut,
    ReceiptItemOut,
)
from support_agent.claim_schemas import ClaimItemRequest, CreateClaimInput
from support_agent.orders import Order

logger = logging.getLogger("ax.support.analysis")

MAX_RECEIPT_TEXT = 8000
ITEM_NAME_RATIO = 0.6  # 고객이 말한 품목 이름이 주문 품목과 같다고 보는 유사도

# LLM을 쓸 수 없을 때(규칙 대체) 쓰는 단어. 분류가 틀릴 수 있어 `source: rules`로 표시한다.
_EVENT_WORDS = (
    "이벤트",
    "프로모션",
    "쿠폰",
    "적립",
    "event",
    "promotion",
    "coupon",
    "イベント",
    "活动",
)
_KIND_WORDS = (
    ("exchange", ("교환", "exchange", "交換", "换货")),
    ("cancel", ("취소", "cancel", "キャンセル", "取消")),
    ("refund", ("환불", "반품", "refund", "return", "返金", "返品", "退款", "退货")),
)
_REASON_WORDS = (
    ("damaged", ("파손", "찢어", "damaged", "torn", "破損", "损坏", "破损")),
    ("defect", ("불량", "고장", "defect", "broken", "不良", "故障")),
    ("wrong_item", ("오배송", "잘못 왔", "wrong item", "wrong product", "違う商品", "发错")),
    ("missing_item", ("누락", "빠져", "missing", "欠品", "缺少")),
    ("late_delivery", ("늦", "지연", "안 와", "late", "delayed", "遅", "延迟")),
    ("change_of_mind", ("변심", "마음에 안", "단순", "changed my mind", "気が変わ", "不想要")),
)


def _similar(a: str, b: str) -> float:
    a, b = a.casefold().strip(), b.casefold().strip()
    if a and (a in b or b in a):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _contains(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(w.casefold() in lowered for w in words)


# ---------------------------------------------------------------- 영수증
def _images(request: AgentRequest) -> tuple[list[EvidenceImageIn], str | None]:
    """`untrusted.images`를 검사한다. 개수·크기를 넘거나 형식이 다르면 `(빈 목록, 이유)`다."""
    raw = request.untrusted.get("images")
    if not raw:
        return [], None
    if not isinstance(raw, list) or len(raw) > MAX_IMAGES:
        return [], f"사진은 최대 {MAX_IMAGES}장까지만 읽는다"
    try:
        images = [EvidenceImageIn.model_validate(item) for item in raw]
    except ValidationError:
        return [], "사진 형식이 맞지 않아 읽지 않았다 (jpeg·png·webp, 한 장 약 4.5MB 이하)"
    if sum(len(i.data_base64 or "") for i in images) > MAX_IMAGE_BASE64 * 2:
        return [], "사진 크기가 커서 읽지 않았다"
    return images, None


def _merge(base: receipts.ReceiptFacts, extra: receipts.ReceiptFacts, notes: list[str]) -> None:
    """글에서 못 찾은 값만 사진에서 채운다. 둘이 다르면 글을 따르고 알린다."""
    for name in ("total", "paid_on", "order_no", "shipping_fee"):
        mine, other = getattr(base, name), getattr(extra, name)
        if mine is None and other is not None:
            setattr(base, name, other)
        elif mine is not None and other is not None and mine != other:
            notes.append(f"글과 사진의 {name} 값이 달라 글 값을 썼다")
    if not base.items and extra.items:
        base.items = extra.items


def _facts_out(facts: receipts.ReceiptFacts) -> ReceiptFactsOut:
    return ReceiptFactsOut(
        total=facts.total,
        paid_on=facts.paid_on,
        order_no=facts.order_no,
        shipping_fee=facts.shipping_fee,
        items=[ReceiptItemOut(title=i.title, qty=i.qty, amount=i.amount) for i in facts.items],
        ambiguous_total=facts.ambiguous_total,
    )


def _order_out(order: Order) -> OrderFactsOut:
    cash, gross = receipts.order_totals(order)
    return OrderFactsOut(
        order_id=order.id,
        cash_paid=cash,
        gross_total=gross,
        shipping_fee=order.shipping_fee,
        paid_on=order.paid_at.astimezone(receipts.KST).date(),
        item_titles=[i.title for i in order.items],
    )


async def _inquiry_order(
    request: AgentRequest, inquiry_id: int, order_id: str | None
) -> tuple[Any, Order] | AgentResult:
    """문의와 그 문의에 붙은 주문. 없거나 다른 주문을 지정하면 결과(오류)를 돌려준다."""
    inquiry = h.STORE.get(inquiry_id)
    if inquiry is None:
        return h._not_found(request, inquiry_id)
    if not inquiry.order_id:
        return h._done(
            request, {"message": "문의에 주문이 없어 분석할 수 없다"}, outcome="no_order"
        )
    if order_id is not None and order_id != inquiry.order_id:
        return error_result(
            request.meta, TaskStatus.FAILED, "invalid_input", "문의에 붙은 주문만 분석한다"
        )
    try:
        order = await shop_client.load_order(inquiry.order_id)
    except shop_client.ShopError as exc:
        return error_result(request.meta, TaskStatus.FAILED, "shop_unavailable", str(exc))
    if order is None:
        return h._not_found(request, inquiry_id)
    return inquiry, order


async def analyze_receipt(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, AnalyzeReceiptInput)
    if isinstance(parsed, AgentResult):
        return parsed
    found = await _inquiry_order(request, parsed.inquiry_id, parsed.order_id)
    if isinstance(found, AgentResult):
        return found
    inquiry, order = found

    raw_text = request.untrusted.get("receipt_text")
    text = raw_text if isinstance(raw_text, str) else ""
    images, image_problem = _images(request)
    if not text.strip() and not images and not image_problem:
        return error_result(
            request.meta,
            TaskStatus.FAILED,
            "invalid_input",
            "untrusted.receipt_text 또는 untrusted.images가 필요하다",
        )

    notes: list[str] = []
    pii_kinds: list[str] = []
    facts = receipts.ReceiptFacts()
    if text.strip():
        masked, pii_kinds = guard.mask_pii(text[:MAX_RECEIPT_TEXT])  # 가린 글만 읽는다
        facts = receipts.parse_receipt_text(masked)
        if len(text) > MAX_RECEIPT_TEXT:
            notes.append(f"영수증 글이 길어 앞 {MAX_RECEIPT_TEXT}자만 읽었다")

    status = "none"
    if image_problem:
        status = "skipped"
        notes.append(image_problem)
    elif images:
        extractor = receipts.get_vision_extractor()
        if extractor is None:
            status = "unavailable"
            notes.append("이미지 분석이 연결되지 않아 사진은 읽지 못했다. 상담원이 직접 확인한다")
        else:
            try:
                extra = await extractor(
                    [receipts.EvidenceImage(i.ref, i.media_type, i.data_base64) for i in images]
                )
                _merge(facts, extra, notes)
                status = "read"
            except Exception:
                logger.warning("영수증 사진 분석 실패", exc_info=True)
                status = "failed"
                notes.append("사진을 읽다가 실패했다. 상담원이 직접 확인한다")

    if facts.ambiguous_total:
        notes.append("총 결제 금액 후보가 서로 달라 금액은 대조하지 않았다")
    if pii_kinds:
        notes.append("영수증에 개인정보가 있어 가린 뒤 읽었다 (원문은 저장하지 않는다)")
    check = receipts.compare(facts, order)
    output = AnalyzeReceiptOutput(
        facts=_facts_out(facts),
        order=_order_out(order),
        check=ReceiptCheckOut(**check.__dict__),
        images_status=status,  # type: ignore[arg-type]
        pii_masked=pii_kinds,
        notes=notes,
    )
    return h._done(
        request,
        output.model_dump(mode="json"),
        outcome=f"receipt_{check.verdict}",
        evidence=Evidence(
            queries=["shop_order"],
            ref_ids=[f"inquiry:{inquiry.id}", f"order:{order.id}"],
            data_summary=(
                f"영수증 대조 {check.verdict}: {', '.join(check.mismatches) or '불일치 없음'}"
            ),
        ),
    )


# ---------------------------------------------------------------- 환불·교환 요청
def classify_by_rules(text: str) -> LLMClaimRequest:
    """LLM 없이 단어로 분류한다 (대체 경로). 틀릴 수 있어 출력에 `source: rules`를 붙인다."""
    kind = next((name for name, words in _KIND_WORDS if _contains(text, words)), None)
    event = _contains(text, _EVENT_WORDS)
    if kind is None:
        kind = "event_inquiry" if event else "other"
    reason = next((name for name, words in _REASON_WORDS if _contains(text, words)), None)
    keywords = [w for w in _EVENT_WORDS if w.casefold() in text.casefold()] if event else []
    return LLMClaimRequest(
        request_kind=kind,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        event_keywords=drafting.clean_keywords(keywords),
    )


async def _classify(text: str) -> tuple[LLMClaimRequest, str]:
    try:
        result = await drafting.get_llm().structured(
            "고객의 환불·교환·취소 요청을 분류하라. request_kind는 refund·exchange·cancel·"
            "event_inquiry(이벤트·쿠폰·프로모션 문의)·other 중 하나다. reason은 change_of_mind·"
            "defect·wrong_item·damaged·missing_item·late_delivery·other 중 알 수 있을 때만 쓰고, "
            "고객이 말한 상품 이름은 items_mentioned에, 이벤트·쿠폰 관련 단어는 event_keywords에 "
            "넣는다. 금액이나 환불 가능 여부는 판단하지 않는다.",
            LLMClaimRequest,
            untrusted={"customer_message": text},
        )
        return result, "llm"
    except Exception:
        logger.warning("요청 분류 LLM 실패, 규칙으로 대체한다", exc_info=True)
        return classify_by_rules(text), "rules"


def _match_items(mentioned: list[str], order: Order) -> tuple[list[Any], list[str]]:
    """고객이 말한 품목을 주문 품목과 맞춘다. 말하지 않았으면 주문 전체다."""
    if not mentioned:
        return list(order.items), []
    chosen: list[Any] = []
    unmatched: list[str] = []
    for name in mentioned:
        best = max(order.items, key=lambda i: _similar(name, i.title), default=None)
        if best is not None and _similar(name, best.title) >= ITEM_NAME_RATIO:
            if best not in chosen:
                chosen.append(best)
        else:
            unmatched.append(name)
    return chosen, unmatched


def _claim_type(kind: str, order: Order) -> str | None:
    if kind == "exchange":
        return "exchange"
    if kind == "cancel":
        return "cancel"
    if kind == "refund":  # 배송 전이면 취소, 배송 뒤면 반품 환불이다 (규칙과 같은 기준)
        return "return" if order.delivered_at is not None else "cancel"
    return None


def _preview(order: Order, claim_type: str, reason: str, items: list[Any]) -> ClaimPreview:
    wanted = {i.id: i.qty for i in items}
    decision = claim_rules.decide(
        order,
        claim_type,
        reason,
        items=wanted,
        evidence_count=0,  # 증빙은 접수할 때 센다. 필요하면 규칙이 증빙 필요를 알려 준다
        unsealed=False,
        prior_claims=claim_handlers.CLAIMS.count_for_buyer(order.buyer_id),
        now=datetime.now(UTC),
    )
    return ClaimPreview(
        outcome=decision.outcome,
        resolution=decision.resolution,
        requires_return=decision.requires_return,
        expected_refund=decision.expected_refund,
        shipping_refund=decision.shipping_refund,
        reasons=[
            PreviewReason(code=r.code, level=r.level, message=r.message) for r in decision.reasons
        ],
        lines=[
            PreviewLine(
                order_item_id=line.order_item_id,
                title=line.title,
                qty=line.requested_qty,
                refund_cash=line.refund_cash,
            )
            for line in decision.lines
            if line.requested_qty
        ],
    )


async def analyze_claim_request(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, AnalyzeClaimRequestInput)
    if isinstance(parsed, AgentResult):
        return parsed
    inquiry = h.STORE.get(parsed.inquiry_id)
    if inquiry is None:
        return h._not_found(request, parsed.inquiry_id)
    customer = [m for m in inquiry.messages if m.role == "customer"]
    if not customer:
        return h._done(request, {"message": "분석할 고객 메시지가 없다"}, outcome="no_message")
    text, _ = guard.mask_pii(customer[-1].text)

    result, source = await _classify(text)
    notes: list[str] = []
    if source == "rules":
        notes.append("LLM을 쓸 수 없어 단어 규칙으로 분류했다. 담당자가 확인한다")
    output = AnalyzeClaimRequestOutput(
        request_kind=result.request_kind,
        reason=result.reason,
        event_related=result.request_kind == "event_inquiry" or bool(result.event_keywords),
        event_keywords=drafting.clean_keywords(result.event_keywords),
        source=source,  # type: ignore[arg-type]
        notes=notes,
        policy_sources=[
            PolicySource(id=s.id, title=s.title, section=s.section)
            for s in kb.search(text, limit=3)
        ],
    )

    if inquiry.order_id:
        try:
            order = await shop_client.load_order(inquiry.order_id)
        except shop_client.ShopError as exc:
            return error_result(request.meta, TaskStatus.FAILED, "shop_unavailable", str(exc))
        if order is not None:
            items, unmatched = _match_items(result.items_mentioned, order)
            output.items = [i.title for i in items]
            output.unmatched_items = unmatched
            claim_type = _claim_type(result.request_kind, order)
            if unmatched:
                output.notes.append("고객이 말한 품목 중 주문에서 찾지 못한 것이 있다")
            if claim_type and items:
                reason = result.reason or "other"
                output.claim_type = claim_type  # type: ignore[assignment]
                output.preview = _preview(order, claim_type, reason, items)
                output.suggested_claim = CreateClaimInput(
                    buyer_id=order.buyer_id,
                    customer_name=inquiry.customer_name,
                    order_id=order.id,
                    type=claim_type,  # type: ignore[arg-type]
                    reason=reason,  # type: ignore[arg-type]
                    items=None
                    if len(items) == len(order.items)
                    else [ClaimItemRequest(order_item_id=i.id, qty=i.qty) for i in items],
                    conversation_id=inquiry.id,
                )
                if result.reason is None:
                    output.notes.append("사유를 알 수 없어 other로 계산했다. 사유를 확인한다")
        else:
            output.notes.append("주문을 찾을 수 없어 환불 범위를 계산하지 못했다")
    else:
        output.notes.append("문의에 주문이 없어 환불 범위를 계산하지 못했다")

    return h._done(
        request,
        output.model_dump(mode="json"),
        outcome="analyzed",
        evidence=Evidence(
            queries=["claim_rules"] if output.preview else [],
            ref_ids=[f"inquiry:{inquiry.id}"]
            + ([f"order:{inquiry.order_id}"] if inquiry.order_id else []),
            data_summary=f"{output.request_kind} / {output.reason or '사유 미상'} ({source})",
        ),
    )


ANALYSIS_HANDLERS = {
    "analyze_receipt": analyze_receipt,
    "analyze_claim_request": analyze_claim_request,
}
