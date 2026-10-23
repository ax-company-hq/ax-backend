"""대시보드 인사 화면이 보여 줄 현황 조회 skill 6개(모두 읽기 전용, 관문을 거치지 않는다).

- `list_handovers` · `list_open_questions` · `list_onboardings` · `list_changes`
- `list_candidate_cards` · `get_audit_timeline`
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
    ChangeView,
    ChecklistItem,
    GetAuditTimelineInput,
    GetAuditTimelineOutput,
    HandoverView,
    ListCandidateCardsInput,
    ListCandidateCardsOutput,
    ListChangesInput,
    ListChangesOutput,
    ListHandoversInput,
    ListHandoversOutput,
    ListOnboardingsInput,
    ListOnboardingsOutput,
    ListOpenQuestionsInput,
    ListOpenQuestionsOutput,
    OnboardingView,
)
from hr_agent.handover import store as hstore
from hr_agent.onboarding.service import progress_of, steps_of
from hr_agent.results import completed, invalid_input, make_evidence
from hr_agent.services import Services
from hr_agent.util import loads


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


async def list_onboardings(svc: Services, request: AgentRequest) -> AgentResult:
    params = _parse_input(ListOnboardingsInput, request)
    if isinstance(params, AgentResult):
        return params
    rows = svc.store.all("SELECT * FROM onboardings ORDER BY created_at DESC, onboarding_id")
    views = []
    for r in rows:
        if params.member_id and r["member_id"] != params.member_id:
            continue
        if params.area and r["area"] != params.area:
            continue
        steps = steps_of(svc, r["onboarding_id"])
        views.append(
            OnboardingView(
                onboarding_id=r["onboarding_id"],
                context_id=r["context_id"],
                member_id=r["member_id"],
                area=r["area"],
                handover_id=r["handover_id"],
                steps=steps,
                progress=progress_of(steps),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
        )
    output = ListOnboardingsOutput(onboardings=views, collected_at=svc.clock())
    return _done(
        svc, request, output, f"온보딩 {len(views)}건 조회", [v.onboarding_id for v in views]
    )


async def list_changes(svc: Services, request: AgentRequest) -> AgentResult:
    params = _parse_input(ListChangesInput, request)
    if isinstance(params, AgentResult):
        return params
    rows = svc.store.all("SELECT * FROM assignment_changes ORDER BY updated_at DESC, change_id")
    views = []
    for r in rows:
        if params.status and r["status"] != params.status:
            continue
        if params.area and r["area"] != params.area:
            continue
        if params.member_id and r["member_id"] != params.member_id:
            continue
        views.append(
            ChangeView(
                change_id=r["change_id"],
                context_id=r["context_id"],
                change_kind=r["kind"],
                member_id=r["member_id"],
                area=r["area"],
                successor_id=r["successor_id"],
                status=r["status"],
                handover_id=r["handover_id"],
                onboarding_id=r["onboarding_id"],
                checklist=[ChecklistItem(**c) for c in loads(r["checklist"], [])],
                created_at=datetime.fromisoformat(r["created_at"]),
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
        )
    output = ListChangesOutput(changes=views, collected_at=svc.clock())
    return _done(
        svc, request, output, f"변경 요청 {len(views)}건 조회", [v.change_id for v in views]
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
