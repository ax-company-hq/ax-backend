"""클레임(취소·반품·교환) skill 핸들러.

- 판정은 `claim_rules.decide`가 주문 사실에서 코드로 한다. 입력에는 금액이 없고 LLM도 쓰지 않는다.
- 읽기 skill은 상태를 바꾸지 않는다. 쓰기 skill은 `handlers._write`(멱등 확인 → `execute()` →
  저장소 반영)를 거친다. 실행되지 않으면(보류·검증 중·거부) 저장소를 바꾸지 않는다.
- 환불 지급(`process_claim_queue`)은 safety 레지스트리의 `refund_request`(B, 재무 검증, 기준 이상
  C)로 실행한다. TODO: 실행 뒤 shop-api 환불 API(SHOP-12) 호출. 지금은 지급 기록만 남긴다.
- 보상 쿠폰은 클레임이 아니라 `request_compensation`(DEP-CS-02)으로 요청한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ax_agent_template.execute import execute
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence
from support_agent import claim_rules, orders
from support_agent import handlers as h
from support_agent.claim_schemas import (
    ClaimReasonHit,
    CreateClaimInput,
    DecideClaimInput,
    GetClaimInput,
    InspectReturnInput,
    ListClaimsInput,
    ProcessClaimQueueInput,
    QueueRun,
    QueueRunResult,
    ReceiveReturnInput,
)
from support_agent.claims import ClaimStore, now_iso

CLAIMS = ClaimStore()
# 지급을 실행하지 못한 이유 (safety 판정 상태 → 화면에 보일 문장)
HELD_MESSAGE = {
    "verifying": "재무 교차검증 중",
    "held": "안전 레이어 보류 (사람 확인 대기)",
    "rejected": "실행이 거부됨",
    "failed": "실행에 실패함",
}


def _note(request: AgentRequest) -> str | None:
    """상담원·검수 메모는 `untrusted.note`로만 받는다. 저장만 하고 지시로 쓰지 않는다."""
    note = request.untrusted.get("note")
    return note.strip() or None if isinstance(note, str) else None


def _claim_not_found(request: AgentRequest, claim_id: int) -> AgentResult:
    return h._done(request, {"message": f"클레임 {claim_id}를 찾을 수 없다"}, outcome="not_found")


def _invalid_state(request: AgentRequest, claim_id: int, status: str, wanted: str) -> AgentResult:
    return h._done(
        request,
        {"message": f"클레임 {claim_id}는 {status} 상태라 {wanted}할 수 없다", "status": status},
        outcome="invalid_state",
    )


# ---------------------------------------------------------------- 읽기
async def list_claims(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, ListClaimsInput)
    if isinstance(parsed, AgentResult):
        return parsed
    items = CLAIMS.list(parsed.status)
    return h._done(request, {"items": [c.model_dump(mode="json") for c in items]})


async def get_claim(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, GetClaimInput)
    if isinstance(parsed, AgentResult):
        return parsed
    claim = CLAIMS.get(parsed.claim_id)
    if claim is None:
        return _claim_not_found(request, parsed.claim_id)
    return h._done(request, claim.model_dump(mode="json"))


async def claim_stats(request: AgentRequest) -> AgentResult:
    return h._done(request, CLAIMS.stats().model_dump(mode="json"))


async def claim_queue(request: AgentRequest) -> AgentResult:
    return h._done(request, CLAIMS.queue().model_dump(mode="json"))


# ---------------------------------------------------------------- 쓰기 (execute 경유)
async def create_claim(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, CreateClaimInput)
    if isinstance(parsed, AgentResult):
        return parsed
    order = orders.get_order(parsed.order_id)
    if order is None or order.user_id != parsed.user_id:
        return h._done(
            request, {"message": f"주문 {parsed.order_id}를 찾을 수 없다"}, outcome="not_found"
        )
    key = request.meta.idempotency_key or (
        f"{request.meta.context_id}:create_claim:order:{order.id}:{parsed.type}"
    )
    if key in h.STORE.results:  # 같은 요청의 재시도는 중복 검사보다 먼저 이전 결과를 돌려준다
        return h._done(request, h.STORE.results[key])
    wanted = None if parsed.items is None else {i.order_item_id: i.qty for i in parsed.items}
    item_ids = {i.id for i in order.items} if wanted is None else set(wanted)
    if CLAIMS.has_open_claim(order.id, item_ids):
        return h._done(
            request,
            {"message": "같은 품목에 진행 중인 클레임이 있다"},
            outcome="duplicate_claim",
        )
    decision = claim_rules.decide(
        order,
        parsed.type,
        parsed.reason,
        items=wanted,
        evidence_count=parsed.evidence_count,
        unsealed=parsed.unsealed,
        prior_claims=CLAIMS.count_for_user(parsed.user_id),
        now=datetime.now(UTC),
    )

    async def apply() -> tuple[dict[str, Any], bool]:
        claim = CLAIMS.create(
            context_id=request.meta.context_id,
            tenant_id=request.meta.tenant_id,
            order=order,
            customer=parsed.customer_name,
            claim_type=parsed.type,
            reason=parsed.reason,
            decision=decision,
            evidence_count=parsed.evidence_count,
            unsealed=parsed.unsealed,
            conversation_id=parsed.conversation_id,
        )
        return claim.model_dump(mode="json"), False

    return await h._write(
        request,
        "create_claim",
        f"order:{order.id}:{parsed.type}",
        {"order_id": order.id, "type": parsed.type, "reason": parsed.reason},
        apply,
    )


async def decide_claim(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, DecideClaimInput)
    if isinstance(parsed, AgentResult):
        return parsed
    claim = CLAIMS.get(parsed.claim_id)
    if claim is None:
        return _claim_not_found(request, parsed.claim_id)
    allowed = ("needs_review",) if parsed.decision == "approve" else ("needs_review", "needs_info")
    if claim.status not in allowed:
        return _invalid_state(request, claim.id, claim.status, "결정")

    async def apply() -> tuple[dict[str, Any], bool]:
        if parsed.decision == "approve":
            status = "awaiting_return" if claim.requires_return else "approved"
        else:
            status = "rejected"
        claim.decided_by = request.meta.requester or "agent_console"
        claim.agent_note = _note(request)
        CLAIMS.set_status(
            claim,
            status,
            parsed.decision,
            note=claim.agent_note or "",
            system_outcome=claim.outcome,
            agent_action=parsed.decision,
        )
        return claim.model_dump(mode="json"), False

    return await h._write(
        request,
        "decide_claim",
        f"claim:{claim.id}:{parsed.decision}",
        {"claim_id": claim.id, "decision": parsed.decision},
        apply,
    )


async def receive_return(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, ReceiveReturnInput)
    if isinstance(parsed, AgentResult):
        return parsed
    claim = CLAIMS.get(parsed.claim_id)
    if claim is None:
        return _claim_not_found(request, parsed.claim_id)
    if claim.status != "awaiting_return":
        return _invalid_state(request, claim.id, claim.status, "입고 처리")

    async def apply() -> tuple[dict[str, Any], bool]:
        claim.return_received_at = now_iso()
        claim.inspection = parsed.inspection
        claim.inspection_note = _note(request)
        CLAIMS.set_status(claim, "return_received", "receive", inspection=parsed.inspection)
        return claim.model_dump(mode="json"), False

    return await h._write(
        request, "receive_return", f"claim:{claim.id}", {"claim_id": claim.id}, apply
    )


async def inspect_return(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, InspectReturnInput)
    if isinstance(parsed, AgentResult):
        return parsed
    claim = CLAIMS.get(parsed.claim_id)
    if claim is None:
        return _claim_not_found(request, parsed.claim_id)
    if claim.status != "return_received":
        return _invalid_state(request, claim.id, claim.status, "검수")

    async def apply() -> tuple[dict[str, Any], bool]:
        claim.inspection = parsed.inspection
        claim.inspection_note = _note(request)
        CLAIMS.set_status(claim, "return_received", "inspection", inspection=parsed.inspection)
        return claim.model_dump(mode="json"), False

    return await h._write(
        request,
        "inspect_return",
        f"claim:{claim.id}:{parsed.inspection}",
        {"claim_id": claim.id, "inspection": parsed.inspection},
        apply,
    )


async def process_claim_queue(request: AgentRequest) -> AgentResult:
    """준비된 큐 항목을 `refund_request`로 지급한다. 항목마다 따로 `execute()`를 거친다."""
    parsed = h._parse(request, ProcessClaimQueueInput)
    if isinstance(parsed, AgentResult):
        return parsed
    results: list[QueueRunResult] = []
    paid = held = 0
    for entry in CLAIMS.queue().queue:
        if parsed.claim_ids is not None and entry.claim_id not in parsed.claim_ids:
            continue
        if parsed.limit is not None and len(results) >= parsed.limit:
            break
        claim = CLAIMS.get(entry.claim_id)
        if claim is None:
            continue
        if not entry.ready:
            results.append(
                QueueRunResult(
                    claim_id=claim.id,
                    result="skipped",
                    detail=", ".join(b.message for b in entry.blockers),
                    blockers=entry.blockers,
                )
            )
            continue
        # 키와 사건은 클레임 단위로 고정한다. 안전 레이어는 C 보류 건을 승인한 뒤 같은 키로 다시
        # 요청해야 `executed`를 돌려주는데, 호출마다 새 키를 쓰면 승인이 반영되지 않는다.
        meta = request.meta.model_copy(
            update={
                "context_id": claim.context_id,
                "idempotency_key": f"{claim.context_id}:refund_request:claim:{claim.id}",
            }
        )
        decision = await execute(
            "refund_request",
            {
                "claim_id": claim.id,
                "order_id": claim.order_id,
                "buyer_id": f"usr_{claim.user_id}",
                "refund_amount": claim.expected_refund,
            },
            meta,
            Evidence(
                queries=["claim_rules"],
                data_summary=f"claim {claim.id}: 환불 {claim.expected_refund}원",
                ref_ids=[
                    f"claim:{claim.id}",
                    f"order:{claim.order_id}",
                    f"request:{request.meta.context_id}",
                ],
            ),
        )
        if decision.status != "executed":
            held += 1
            results.append(
                QueueRunResult(
                    claim_id=claim.id,
                    result="held",
                    detail=decision.status,
                    blockers=[
                        ClaimReasonHit(
                            code="ON_HOLD",
                            level="block",
                            message=HELD_MESSAGE.get(decision.status, decision.status),
                        )
                    ],
                )
            )
            continue
        # TODO: shop-api 환불 API(SHOP-12) 호출. 지금은 실행 허가와 지급 기록만 남긴다
        claim.refund_paid = claim.expected_refund
        claim.processed_at = now_iso()
        CLAIMS.set_status(
            claim,
            "completed",
            "pay",
            amount=claim.expected_refund,
            refund_cash=claim.expected_refund,
            compensation=0,
            execution=decision.execution_id,
        )
        paid += 1
        results.append(QueueRunResult(claim_id=claim.id, result="paid", refund=claim.refund_paid))
    run = QueueRun(results=results, paid=paid, held=held, queue=CLAIMS.queue().queue)
    return h._done(request, run.model_dump(mode="json"))


CLAIM_HANDLERS = {
    "list_claims": list_claims,
    "get_claim": get_claim,
    "claim_stats": claim_stats,
    "claim_queue": claim_queue,
    "create_claim": create_claim,
    "decide_claim": decide_claim,
    "receive_return": receive_return,
    "inspect_return": inspect_return,
    "process_claim_queue": process_claim_queue,
}
