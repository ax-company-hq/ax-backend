"""기획개발 변경 요청 skill: `request_change`(보내기), `list_change_requests`(읽기).

변경 요청은 고객지원이 기획개발에 직통으로 보낸다(2-2, 통합 에이전트를 거치지 않음). 기획개발이
자기 `execute()`를 거쳐 요청을 만들고 `ctx_req_`를 발급해 돌려주면, 그 사건 번호를 문의와 연결해
저장한다(`change_requests.py`). 같은 요청이 이미 있으면 기획개발이 묶어서(`linked_duplicate`) 같은
`ctx_req_`를 돌려주므로 한 사건에 문의가 여러 개 붙을 수 있다.

결과 통지(처리됨·거절됨)를 받는 skill은 계약이 정해지면 붙인다. 지금은 저장소의
`record_result`·`attach_notice`까지만 준비돼 있다.
"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError

from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from ax_contracts.schemas.planning import CreateTicketOutput
from support_agent import guard
from support_agent import handlers as h
from support_agent.change_requests import source_ref_for
from support_agent.change_schemas import (
    ListChangeRequestsInput,
    ListChangeRequestsOutput,
    RequestChangeInput,
    RequestChangeOutput,
)

PLANNING = "planning"


def _digest(parsed: RequestChangeInput) -> str:
    basis = f"{parsed.title}\n{parsed.evidence}\n{sorted(parsed.evidence_ids)}"
    return hashlib.sha1(basis.encode()).hexdigest()[:8]


async def request_change(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, RequestChangeInput)
    if isinstance(parsed, AgentResult):
        return parsed
    if h.STORE.get(parsed.inquiry_id) is None:
        return h._not_found(request, parsed.inquiry_id)
    for field, text in (("title", parsed.title), ("evidence", parsed.evidence)):
        if guard.find_pii(text):  # 기획개발 활동 기록에 개인정보가 남지 않게 막는다
            return error_result(
                request.meta, TaskStatus.FAILED, "invalid_input", f"{field}에 개인정보가 있다"
            )
    key = request.meta.idempotency_key or (
        f"{request.meta.context_id}:request_change:inquiry:{parsed.inquiry_id}:{_digest(parsed)}"
    )
    if key in h.STORE.results:  # 같은 요청의 재시도
        return h._done(request, h.STORE.results[key])

    ticket = AgentRequest(
        meta=request.meta.model_copy(update={"idempotency_key": key}),
        skill="create_ticket",
        input={
            "title": parsed.title,
            "evidence": parsed.evidence,
            "source_ref": source_ref_for(parsed.inquiry_id),
            "evidence_ids": parsed.evidence_ids,
        },
    )
    reply = await h.SEND(PLANNING, ticket)
    if reply.status != TaskStatus.COMPLETED:
        return error_result(
            request.meta,
            TaskStatus.FAILED,
            "planning_unavailable",
            f"기획개발이 요청을 처리하지 못했다 ({reply.outcome or reply.status.value})",
        )
    if reply.outcome not in ("created", "linked_duplicate"):
        message = str(reply.output.get("message", "")) if reply.output else ""
        return h._done(
            request,
            {"planning_outcome": reply.outcome, "message": message},
            outcome=f"planning_{reply.outcome}",
        )
    try:
        ticket_out = CreateTicketOutput.model_validate(reply.output)
    except ValidationError as exc:
        return error_result(request.meta, TaskStatus.FAILED, "invalid_output", str(exc)[:200])

    link = h.CHANGES.link(
        ticket_out.context_id,
        parsed.inquiry_id,
        request_id=ticket_out.request_id,
        duplicate_of=ticket_out.duplicate_of,
        tenant_id=request.meta.tenant_id,
    )
    output = RequestChangeOutput(
        outcome=ticket_out.outcome,
        request_id=ticket_out.request_id,
        context_id=ticket_out.context_id,
        duplicate_of=ticket_out.duplicate_of,
        link=link,
    ).model_dump(mode="json")
    h.STORE.results[key] = output
    return h._done(
        request,
        output,
        outcome=ticket_out.outcome,
        evidence=Evidence(
            ref_ids=[f"inquiry:{parsed.inquiry_id}", ticket_out.request_id, ticket_out.context_id],
            data_summary=f"변경 요청 {ticket_out.request_id} ({ticket_out.outcome})",
        ),
    )


async def list_change_requests(request: AgentRequest) -> AgentResult:
    parsed = h._parse(request, ListChangeRequestsInput)
    if isinstance(parsed, AgentResult):
        return parsed
    if parsed.context_id is not None:
        items = h.CHANGES.for_context(parsed.context_id)
    elif parsed.inquiry_id is not None:
        items = h.CHANGES.for_inquiry(parsed.inquiry_id)
    else:
        items = h.CHANGES.all()
    if parsed.status is not None:
        items = [link for link in items if link.status == parsed.status]
    return h._done(request, ListChangeRequestsOutput(items=items).model_dump(mode="json"))


CHANGE_HANDLERS = {
    "request_change": request_change,
    "list_change_requests": list_change_requests,
}
