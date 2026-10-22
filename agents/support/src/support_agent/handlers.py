"""support skill 핸들러 (문의 응대).

- 읽기 skill(`list_inquiries`, `get_inquiry`, `check_reply`, `summarize_inquiry`)은
  상태를 바꾸지 않는다.
- 쓰기 skill은 모두 `execute()`를 거친 뒤에만 저장소를 바꾼다. 같은 `idempotency_key`의 두 번째
  요청은 이전 결과를 돌려준다. 실행되지 않으면(보류·거부) 저장소를 건드리지 않는다.
- 고객 원문(제목·본문)은 `request.untrusted`로만 받고, 다른 곳에 지시로 쓰지 않는다.
- `request_compensation`은 보상 단가를 코드로 판정해 마케팅 `issue_coupon(compensation)`에 직렬
  요청한다. 지급과 `execute()`는 마케팅 쪽이고, support 자신의 상태는 바꾸지 않는다.
- 클레임·현금 환불은 5단계에서 이 파일에 skill로 추가한다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import Any

from pydantic import BaseModel, ValidationError

from ax_agent_template.client import call_agent
from ax_agent_template.execute import execute
from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from support_agent import compensation, drafting, guard, notices
from support_agent.schemas import (
    AddCustomerMessageInput,
    CheckReplyInput,
    CreateInquiryInput,
    GetInquiryInput,
    ListInquiriesInput,
    RejectDraftInput,
    RequestCompensationInput,
    SendReplyInput,
    SetInquiryStatusInput,
    Source,
    SummarizeInquiryInput,
)
from support_agent.store import InquiryStore

logger = logging.getLogger("ax.support.handlers")

STORE = InquiryStore()
LEDGER = compensation.SequenceLedger()
SEND = call_agent  # 테스트에서 바꿀 수 있다


def _parse[M: BaseModel](request: AgentRequest, model: type[M]) -> M | AgentResult:
    try:
        return model.model_validate(request.input)
    except ValidationError as exc:
        return error_result(request.meta, TaskStatus.FAILED, "invalid_input", str(exc))


def _text(request: AgentRequest, key: str) -> str | None:
    value = request.untrusted.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _done(
    request: AgentRequest,
    output: dict[str, Any],
    *,
    outcome: str | None = None,
    fallback: bool = False,
    evidence: Evidence | None = None,
) -> AgentResult:
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=outcome,
        output=output,
        evidence=evidence or Evidence(),
        fallback=fallback,
    )


def _not_found(request: AgentRequest, inquiry_id: int) -> AgentResult:
    return _done(request, {"message": f"문의 {inquiry_id}를 찾을 수 없다"}, outcome="not_found")


def _missing_text(request: AgentRequest, key: str) -> AgentResult:
    return error_result(
        request.meta, TaskStatus.FAILED, "invalid_input", f"untrusted.{key}가 비었다"
    )


async def _write(
    request: AgentRequest,
    action: str,
    target: str,
    params: dict[str, Any],
    apply: Callable[[], Awaitable[tuple[dict[str, Any], bool]]],
) -> AgentResult:
    """상태 변경 공통 경로: 멱등 확인 → `execute()` → 저장소 반영.

    `apply`는 실행이 허용된 뒤에만 불리고 `(output, fallback)`을 돌려준다.
    """
    key = request.meta.idempotency_key or f"{request.meta.context_id}:{action}:{target}"
    if key in STORE.results:
        return _done(request, STORE.results[key])

    meta = request.meta.model_copy(update={"idempotency_key": key})
    decision = await execute(
        action,
        params,
        meta,
        Evidence(ref_ids=[f"inquiry:{target}"], data_summary=f"{action} {target}"),
    )
    if decision.status != "executed":
        # 보류(C)·검증 중(B)·거부는 저장소를 바꾸지 않는다. 결정 내용을 그대로 알린다.
        return _done(
            request,
            {"execution": decision.model_dump(mode="json")},
            outcome=f"not_executed_{decision.status}",
        )

    output, fallback = await apply()
    output = {**output, "execution_id": decision.execution_id}
    STORE.results[key] = output
    return _done(request, output, fallback=fallback)


# ---------------------------------------------------------------- 읽기
async def list_inquiries(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, ListInquiriesInput)
    if isinstance(parsed, AgentResult):
        return parsed
    items = STORE.list(parsed.status)
    return _done(request, {"items": [i.model_dump(mode="json") for i in items]})


async def get_inquiry(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, GetInquiryInput)
    if isinstance(parsed, AgentResult):
        return parsed
    conversation = STORE.get(parsed.inquiry_id)
    if conversation is None:
        return _not_found(request, parsed.inquiry_id)
    return _done(request, conversation.model_dump(mode="json"))


async def check_reply(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, CheckReplyInput)
    if isinstance(parsed, AgentResult):
        return parsed
    text = request.untrusted.get("text")
    if not isinstance(text, str):
        return _missing_text(request, "text")
    return _done(request, guard.check_reply(text).model_dump(mode="json"))


async def summarize_inquiry(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, SummarizeInquiryInput)
    if isinstance(parsed, AgentResult):
        return parsed
    conversation = STORE.get(parsed.inquiry_id)
    if conversation is None:
        return _not_found(request, parsed.inquiry_id)
    summary, fallback = await drafting.summarize(conversation)
    return _done(request, summary.model_dump(mode="json"), fallback=fallback)


# ---------------------------------------------------------------- 쓰기 (execute 경유)
async def _notice_sources(request: AgentRequest, notice_id: str | None) -> list[Source]:
    """문의가 공지를 가리키면 마케팅 `get_notice`로 읽는다. 못 읽으면 근거 없이 진행한다."""
    if notice_id is None:
        return []
    source = await notices.fetch_source(request, notice_id, SEND)
    return [source] if source else []


async def create_inquiry(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, CreateInquiryInput)
    if isinstance(parsed, AgentResult):
        return parsed
    subject, message = _text(request, "subject"), _text(request, "message")
    if subject is None:
        return _missing_text(request, "subject")
    if message is None:
        return _missing_text(request, "message")

    extra = await _notice_sources(request, parsed.notice_id)

    async def apply() -> tuple[dict[str, Any], bool]:
        analysis, fallback = await drafting.analyze(message, subject, extra)
        conversation = STORE.create(
            context_id=request.meta.context_id,
            tenant_id=request.meta.tenant_id,
            user_id=parsed.user_id,
            customer_name=parsed.customer_name,
            subject=subject,
            order_id=parsed.order_id,
            order_no=parsed.order_no,
            first_message=message,
            analysis=analysis,
        )
        return conversation.model_dump(mode="json"), fallback

    return await _write(
        request, "create_inquiry", f"user:{parsed.user_id}", {"user_id": parsed.user_id}, apply
    )


async def add_customer_message(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, AddCustomerMessageInput)
    if isinstance(parsed, AgentResult):
        return parsed
    message = _text(request, "message")
    if message is None:
        return _missing_text(request, "message")
    conversation = STORE.get(parsed.inquiry_id)
    if conversation is None:
        return _not_found(request, parsed.inquiry_id)

    extra = await _notice_sources(request, parsed.notice_id)

    async def apply() -> tuple[dict[str, Any], bool]:
        analysis, fallback = await drafting.analyze(message, conversation.subject, extra)
        return STORE.add_customer_message(parsed.inquiry_id, message, analysis).model_dump(
            mode="json"
        ), fallback

    return await _write(
        request,
        "add_customer_message",
        f"inquiry:{parsed.inquiry_id}",
        {"inquiry_id": parsed.inquiry_id},
        apply,
    )


async def send_reply(request: AgentRequest) -> AgentResult:
    """상담원이 확정한 답변을 고객에게 보낸다. 가드를 통과하지 못하면 보내지 않는다."""
    parsed = _parse(request, SendReplyInput)
    if isinstance(parsed, AgentResult):
        return parsed
    text = _text(request, "text")
    if text is None:
        return _missing_text(request, "text")
    if STORE.get(parsed.inquiry_id) is None:
        return _not_found(request, parsed.inquiry_id)
    validation = guard.check_reply(text)
    if not validation.passed:
        return _done(
            request,
            {"validation": validation.model_dump(mode="json")},
            outcome="blocked_by_guard",
        )

    async def apply() -> tuple[dict[str, Any], bool]:
        conversation = STORE.add_agent_message(parsed.inquiry_id, text)
        if parsed.draft_style:
            draft = (parsed.draft_text or "").strip()
            edited = parsed.draft_text is not None and draft != text
            STORE.draft_events.append(
                {
                    "inquiry_id": parsed.inquiry_id,
                    "style": parsed.draft_style,
                    "result": "edited" if edited else "accepted",
                    # 초안과 보낸 답변이 다른 정도 (0 같음 ~ 1 완전히 다름). 수정한 경우만 쓴다
                    "edit_ratio": round(1 - SequenceMatcher(None, draft, text).ratio(), 3)
                    if edited
                    else None,
                }
            )
        return conversation.model_dump(mode="json"), False

    return await _write(
        request,
        "send_reply",
        f"inquiry:{parsed.inquiry_id}",
        {"inquiry_id": parsed.inquiry_id},
        apply,
    )


async def reject_draft(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, RejectDraftInput)
    if isinstance(parsed, AgentResult):
        return parsed
    if STORE.get(parsed.inquiry_id) is None:
        return _not_found(request, parsed.inquiry_id)

    async def apply() -> tuple[dict[str, Any], bool]:
        STORE.draft_events.append(
            {"inquiry_id": parsed.inquiry_id, "style": parsed.draft_style, "result": "rejected"}
        )
        return {"inquiry_id": parsed.inquiry_id, "draft_style": parsed.draft_style}, False

    return await _write(
        request,
        "reject_draft",
        f"inquiry:{parsed.inquiry_id}:{parsed.draft_style}",
        {"inquiry_id": parsed.inquiry_id},
        apply,
    )


async def set_inquiry_status(request: AgentRequest) -> AgentResult:
    parsed = _parse(request, SetInquiryStatusInput)
    if isinstance(parsed, AgentResult):
        return parsed
    if STORE.get(parsed.inquiry_id) is None:
        return _not_found(request, parsed.inquiry_id)

    async def apply() -> tuple[dict[str, Any], bool]:
        conversation = STORE.set_status(parsed.inquiry_id, parsed.status)
        return conversation.model_dump(mode="json"), False

    return await _write(
        request,
        "set_inquiry_status",
        f"inquiry:{parsed.inquiry_id}:{parsed.status}",
        {"inquiry_id": parsed.inquiry_id, "status": parsed.status},
        apply,
    )


def _sequence_consumed(reply: AgentResult) -> bool:
    """마케팅이 이 `sequence`를 소비했는지 (marketing `issue_coupon`의 기록 규칙과 맞춘다).

    소비한 경우: 지급·재무 거절(`completed`), 보류(`input_required`), 실행 실패·거부(`failed`,
    `execute_*`), 검증 시간 초과(`canceled`, 실행 정보가 output에 있음).
    소비하지 않은 경우: 재무 검증 중(`working`), 번호 오류·입력 오류(`rejected`·그 밖의 `failed`),
    우리 쪽 타임아웃·연결 실패(output 없음). 이때는 같은 번호로 다시 보낸다.
    """
    outcome = str(reply.outcome or "")
    match reply.status:
        case TaskStatus.COMPLETED | TaskStatus.INPUT_REQUIRED:
            return True
        case TaskStatus.FAILED:
            return outcome in ("execute_failed", "execute_rejected")
        case TaskStatus.CANCELED:
            return "execution_id" in reply.output
        case _:
            return False


async def request_compensation(request: AgentRequest) -> AgentResult:
    """보상 단가를 판정해 마케팅 `issue_coupon(compensation)`에 요청한다 (DEP-CS-02)."""
    parsed = _parse(request, RequestCompensationInput)
    if isinstance(parsed, AgentResult):
        return parsed
    if STORE.get(parsed.inquiry_id) is None:
        return _not_found(request, parsed.inquiry_id)

    context_id = request.meta.context_id
    key = request.meta.idempotency_key or (
        f"{context_id}:request_compensation:{parsed.buyer_id}:{parsed.reason_code}"
    )
    if key in LEDGER.results:  # 같은 요청은 이전 결과를 돌려준다
        return _done(request, LEDGER.results[key])

    decision = compensation.decide(parsed.reason_code, parsed.count)
    evidence = Evidence(
        queries=["compensation_policy"],
        data_summary=(
            f"{parsed.reason_code}: {decision.count}건 × {decision.unit_value}원"
            f" = {decision.total_value}원"
            if decision.eligible
            else decision.reason
        ),
        ref_ids=[f"inquiry:{parsed.inquiry_id}"],
    )
    if not decision.eligible:
        output = {"eligible": False, "reason": decision.reason}
        LEDGER.results[key] = output
        return _done(request, output, outcome="not_eligible", evidence=evidence)

    async with LEDGER.lock(context_id):  # 같은 사건의 요청은 한 건씩 (같은 번호를 읽는 경쟁 방지)
        return await _request_coupon(request, parsed, decision, key, evidence)


async def _request_coupon(
    request: AgentRequest,
    parsed: RequestCompensationInput,
    decision: compensation.CompensationDecision,
    key: str,
    evidence: Evidence,
) -> AgentResult:
    context_id = request.meta.context_id
    if key in LEDGER.results:  # 락을 기다리는 사이 같은 요청이 끝났을 수 있다
        return _done(request, LEDGER.results[key])
    sequence = LEDGER.peek(context_id)
    meta = request.meta.model_copy(update={"idempotency_key": key})
    coupon_request = AgentRequest(
        meta=meta,
        skill="issue_coupon",
        input={
            "purpose_code": "compensation",
            "buyer_id": parsed.buyer_id,
            "count": decision.count,
            "unit_value": decision.unit_value,
            "claimed_total": decision.total_value,
        },
        sequence=sequence,
    )
    reply = await SEND("marketing", coupon_request)

    if _sequence_consumed(reply):
        LEDGER.advance(context_id)
    output = {
        "eligible": True,
        "sequence": sequence,
        "count": decision.count,
        "unit_value": decision.unit_value,
        "total_value": decision.total_value,
        "marketing_status": reply.status.value,
        "marketing_outcome": reply.outcome,
        "marketing_output": reply.output,
    }
    if reply.status == TaskStatus.COMPLETED and reply.outcome in (None, "linked_duplicate"):
        LEDGER.results[key] = output
    return _done(request, output, outcome=reply.outcome, evidence=evidence)


HANDLERS = {
    "list_inquiries": list_inquiries,
    "get_inquiry": get_inquiry,
    "check_reply": check_reply,
    "summarize_inquiry": summarize_inquiry,
    "create_inquiry": create_inquiry,
    "add_customer_message": add_customer_message,
    "send_reply": send_reply,
    "reject_draft": reject_draft,
    "set_inquiry_status": set_inquiry_status,
    "request_compensation": request_compensation,
}
