"""`start_onboarding`: 새 담당자가 읽을 순서(결정 카드)와 확인 목록을 만든다.

읽는 순서는 코드 규칙이다: 해당 영역의 유효한 결정 카드를 결정 시각 순으로 나열하고, 인수인계가
있으면 그 내용 확인을, 마지막에 공통 확인 목록(연락처·접근 권한)을 붙인다. 진행률은 끝낸 단계 수로
코드가 계산한다. 카드가 없으면 만들어 내지 않고 "읽을 결정 카드 없음"을 근거에 적는다.
"""

from __future__ import annotations

from pydantic import ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    OnboardingStep,
    StartOnboardingInput,
    StartOnboardingOutput,
)
from hr_agent.clients.memory import card_state
from hr_agent.handover import store as hstore
from hr_agent.results import (
    completed,
    gate_blocked,
    invalid_input,
    make_evidence,
    refused,
    with_key,
)
from hr_agent.roster import store as roster
from hr_agent.services import Services
from hr_agent.util import stable_id

CHECKLIST = ["담당·예비 담당 연락처를 명부에서 확인한다", "영역 접근 권한을 확인한다"]


def progress_of(steps: list[OnboardingStep]) -> int:
    return round(100 * sum(1 for s in steps if s.done) / len(steps)) if steps else 0


async def _area_decisions(svc: Services, area: str):
    cards = [
        c
        for c in await svc.memory.list_cards()
        if (c.extra or {}).get("area") == area
        and card_state(c) == "active"
        and (c.extra or {}).get("decision")
    ]
    return sorted(cards, key=lambda c: ((c.extra or {}).get("decided_at") or "", c.card_id))


def _steps(svc: Services, onboarding_id: str) -> list[OnboardingStep]:
    rows = svc.store.all(
        "SELECT step_id, step_order, title, card_id, done FROM onboarding_steps"
        " WHERE onboarding_id = ? ORDER BY step_order",
        (onboarding_id,),
    )
    return [
        OnboardingStep(
            step_id=r["step_id"],
            order=r["step_order"],
            title=r["title"],
            card_id=r["card_id"],
            done=bool(r["done"]),
        )
        for r in rows
    ]


def _result(
    svc: Services, request: AgentRequest, onboarding_id: str, summary: str, refs: list[str]
):
    steps = _steps(svc, onboarding_id)
    output = StartOnboardingOutput(
        onboarding_id=onboarding_id, steps=steps, progress=progress_of(steps)
    )
    return completed(
        request,
        output,
        outcome="onboarding_started",
        evidence=make_evidence(svc.clock, summary, refs),
    )


async def start_onboarding(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = StartOnboardingInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    if not params.member_id.strip():
        return invalid_input(request, "member_id가 비어 있다")
    if params.area not in roster.list_areas(svc.store):
        return refused(request, "UNKNOWN_AREA", f"알 수 없는 영역: {params.area}")

    handover = None
    if params.handover_id:
        handover = hstore.get(svc.store, params.handover_id)
        if handover is None:
            return refused(request, "UNKNOWN_HANDOVER", "없는 인수인계다")
        if handover["area"] != params.area:
            return invalid_input(request, "인수인계의 영역과 온보딩 영역이 다르다")

    onboarding_id = stable_id("onb_", request.meta.context_id, params.member_id, params.area)
    if svc.store.one("SELECT 1 FROM onboardings WHERE onboarding_id = ?", (onboarding_id,)):
        svc.audit.record(request.meta.context_id, "start_onboarding.duplicate", {})
        return _result(svc, request, onboarding_id, "이미 만들어진 온보딩", [onboarding_id])

    cards = await _area_decisions(svc, params.area)
    titles: list[tuple[str, str | None, bool]] = [
        (f"{params.area} 영역의 담당·교대와 진행 중인 일을 훑어본다", None, False)
    ]
    titles += [(f"결정 읽기: {c.title}", c.card_id, False) for c in cards]
    if handover is not None:
        titles.append(("인수인계 내용을 확인한다", None, handover["status"] == "confirmed"))
    titles += [(t, None, False) for t in CHECKLIST]

    refs = [c.card_id for c in cards]
    summary = (
        f"{params.member_id} 온보딩 ({params.area}): 읽을 결정 카드 {len(cards)}장"
        if cards
        else f"{params.member_id} 온보딩 ({params.area}): 읽을 결정 카드 없음"
    )
    decision = await svc.gate.execute(
        "start_onboarding",
        {"onboarding_id": onboarding_id, "summary": summary, "target": onboarding_id},
        with_key(request.meta, f"start_onboarding:{onboarding_id}"),
        make_evidence(svc.clock, summary, refs),
    )
    if decision.status != "executed":
        return gate_blocked(request, decision)

    now = svc.clock().isoformat()
    with svc.store.transaction():
        svc.store.execute(
            "INSERT INTO onboardings(onboarding_id, context_id, member_id, area, handover_id,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                onboarding_id,
                request.meta.context_id,
                params.member_id,
                params.area,
                params.handover_id,
                now,
            ),
        )
        for order, (title, card_id, done) in enumerate(titles, start=1):
            svc.store.execute(
                "INSERT INTO onboarding_steps(onboarding_id, step_id, step_order, title, card_id,"
                " done) VALUES (?, ?, ?, ?, ?, ?)",
                (onboarding_id, f"stp_{onboarding_id}_{order}", order, title, card_id, int(done)),
            )
    svc.audit.record(
        request.meta.context_id,
        "start_onboarding",
        {"id": onboarding_id, "steps": len(titles), "cards": len(cards)},
    )
    return _result(svc, request, onboarding_id, summary, refs)
