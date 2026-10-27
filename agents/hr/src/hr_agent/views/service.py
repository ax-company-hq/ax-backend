"""대시보드 인사 화면이 보여 줄 현황 조회 skill 4개(모두 읽기 전용, 관문을 거치지 않는다).

- `list_handovers` · `list_open_questions` · `list_candidate_cards` · `get_audit_timeline`
값은 저장된 그대로 돌려주고 새로 계산하거나 지어내지 않는다(진행률·완성도는 저장·계산된 값).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    AuditEntry,
    GetAuditTimelineInput,
    GetAuditTimelineOutput,
    HandoverView,
    ListCandidateCardsInput,
    ListCandidateCardsOutput,
    ListHandoversInput,
    ListHandoversOutput,
    ListOpenQuestionsInput,
    ListOpenQuestionsOutput,
)
from hr_agent.handover import store as hstore
from hr_agent.results import completed, invalid_input, make_evidence
from hr_agent.services import Services


def _parse_input[T: BaseModel](model: type[T], request: AgentRequest) -> T | AgentResult:
    try:
        return model.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)


def _done(
    svc: Services, request: AgentRequest, output: BaseModel, summary: str, refs: list[str]
) -> AgentResult:
    return completed(
        request,
        output,
        outcome="listed",
        evidence=make_evidence(svc.clock, summary, refs),
    )


async def list_handovers(svc: Services, request: AgentRequest) -> AgentResult:
    params = _parse_input(ListHandoversInput, request)
    if isinstance(params, AgentResult):
        return params
    rows = [
        r
        for r in hstore.all_handovers(svc.store)
        if (params.status is None or r["status"] == params.status)
        and (params.area is None or r["area"] == params.area)
    ]
    rows.sort(key=lambda r: (r["updated_at"], r["handover_id"]), reverse=True)
    views = [
        HandoverView(
            handover_id=r["handover_id"],
            context_id=r["context_id"],
            owner_id=r["owner_id"],
            successor_id=r["successor_id"],
            area=r["area"],
            reason=r["reason"],
            status=r["status"],
            completeness=r["completeness"],
            items=hstore.items(svc.store, r["handover_id"]),
            open_questions=hstore.open_questions(svc.store, r["handover_id"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
        )
        for r in rows[: params.limit]
    ]
    output = ListHandoversOutput(handovers=views, collected_at=svc.clock())
    return _done(
        svc, request, output, f"인수인계 {len(views)}건 조회", [v.handover_id for v in views]
    )


async def list_open_questions(svc: Services, request: AgentRequest) -> AgentResult:
    params = _parse_input(ListOpenQuestionsInput, request)
    if isinstance(params, AgentResult):
        return params
    rows = svc.store.all(
        "SELECT q.handover_id, q.question_id FROM handover_questions q JOIN handovers h"
        " ON h.handover_id = q.handover_id WHERE q.status = 'open' AND h.status != 'confirmed'"
        " ORDER BY q.created_at, q.question_id"
    )
    questions = []
    for row in rows:
        if params.handover_id and row["handover_id"] != params.handover_id:
            continue
        for q in hstore.open_questions(svc.store, row["handover_id"]):
            if q.question_id == row["question_id"] and (
                params.assignee_id is None or q.assignee_id == params.assignee_id
            ):
                questions.append(q)
    output = ListOpenQuestionsOutput(questions=questions, collected_at=svc.clock())
    return _done(
        svc,
        request,
        output,
        f"답을 기다리는 질문 {len(questions)}건",
        [q.question_id for q in questions],
    )


async def list_candidate_cards(svc: Services, request: AgentRequest) -> AgentResult:
    params = _parse_input(ListCandidateCardsInput, request)
    if isinstance(params, AgentResult):
        return params
    cards = await svc.memory.list_cards(area=params.area, states=["candidate"])
    output = ListCandidateCardsOutput(cards=cards, collected_at=svc.clock())
    return _done(
        svc,
        request,
        output,
        f"사람 확인을 기다리는 후보 카드 {len(cards)}장",
        [c.card_id for c in cards],
    )


async def get_audit_timeline(svc: Services, request: AgentRequest) -> AgentResult:
    params = _parse_input(GetAuditTimelineInput, request)
    if isinstance(params, AgentResult):
        return params
    entries: list[dict[str, Any]] = svc.audit.timeline(params.context_id)
    output = GetAuditTimelineOutput(
        context_id=params.context_id,
        entries=[
            AuditEntry(
                seq=e["seq"],
                at=datetime.fromisoformat(e["at"]),
                kind=e["kind"],
                payload=e["payload"],
            )
            for e in entries
        ],
        collected_at=svc.clock(),
    )
    summary = (
        f"감사 기록 {len(entries)}건"
        if entries
        else f"{params.context_id}에 대한 감사 기록 없음: 확인할 수 없음"
    )
    return _done(svc, request, output, summary, [params.context_id])
