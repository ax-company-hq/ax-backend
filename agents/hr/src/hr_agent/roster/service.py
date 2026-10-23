"""`get_roster`: 영역별 담당·교대 일정과 공백 영역을 돌려준다(조회 전용, 개인 정보 없음)."""

from __future__ import annotations

from pydantic import ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    GetRosterInput,
    GetRosterOutput,
    RosterEntry,
)
from hr_agent.results import completed, invalid_input, make_evidence
from hr_agent.roster import store as roster
from hr_agent.roster.gaps import compute_gaps
from hr_agent.services import Services


def roster_entries(svc: Services, area: str | None = None) -> list[RosterEntry]:
    now = svc.clock()
    out: list[RosterEntry] = []
    for e in roster.entries(svc.store, area):
        shift = roster.current_or_next_shift(svc.store, e.area, e.member_id, now)
        out.append(
            RosterEntry(
                area=e.area,
                role=e.role,
                member_id=e.member_id,
                shift_start=shift[0] if shift else None,
                shift_end=shift[1] if shift else None,
            )
        )
    return out


async def get_roster(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = GetRosterInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if params.area is not None and params.area not in roster.list_areas(svc.store):
        return invalid_input(request, f"알 수 없는 영역: {params.area}")

    now = svc.clock()
    gaps = compute_gaps(svc.store, now)
    gap_areas = (
        gaps.gap_areas if params.area is None else [a for a in gaps.gap_areas if a == params.area]
    )
    output = GetRosterOutput(
        entries=roster_entries(svc, params.area), gap_areas=gap_areas, collected_at=now
    )
    summary = f"명부 조회: 담당 {len(output.entries)}건, 공백 영역 {len(gap_areas)}곳"
    return completed(
        request,
        output,
        outcome="roster",
        evidence=make_evidence(svc.clock, summary, [f"area:{a}" for a in gap_areas]),
    )
