"""교대 시각 점검(타이머): 곧 끝나는 교대에 이어받는 사람이 있으면 인수인계를 자동으로 시작한다.

- `upcoming_handoffs`: 앞으로 `horizon_minutes` 안에 끝나는 교대 중, 같은 영역에서 그 시각에 다른
  사람이 이어받는 경우. 이어받는 사람이 없는 교대(공백)는 `gaps.compute_gaps`가 따로 알린다.
- `start_due_handovers`: 위 목록마다 `start_handover`를 hr 스스로 요청한다. 사건 번호는 영역·교대
  끝 시각으로 정해 같은 교대에 두 번 만들어지지 않는다(멱등).
- 주기 실행은 `HR_TIMER=1`일 때만 서버 시작 시 켜진다(기본 꺼짐).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ax_contracts.schemas import AgentRequest, RequestMeta
from hr_agent.roster import store as roster
from hr_agent.services import Services, get_services

log = logging.getLogger("hr.timer")


@dataclass(frozen=True)
class Handoff:
    area: str
    from_member: str
    to_member: str
    at: datetime


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def upcoming_handoffs(svc: Services, horizon_minutes: int = 60) -> list[Handoff]:
    now = svc.clock()
    horizon = now + timedelta(minutes=horizon_minutes)
    out: list[Handoff] = []
    for area in roster.list_areas(svc.store):
        spans = [
            (_parse(r["start_at"]), _parse(r["end_at"]), r["member_id"])
            for r in roster.shifts(svc.store, area)
        ]
        for _start, end, member in spans:
            if not (now <= end <= horizon):
                continue
            nxt = sorted(m for s, _e, m in spans if s == end and m != member)
            if nxt:
                out.append(Handoff(area, member, nxt[0], end))
    return sorted(out, key=lambda h: (h.at, h.area, h.from_member))


def shift_context_id(handoff: Handoff) -> str:
    # 시스템이 시작하는 사건 번호. 형식(`ctx_hnd_` 접두사)은 계약 후속 PR에서 확정한다.
    return f"ctx_hnd_shift_{handoff.area}_{handoff.at:%Y%m%d%H%M}"


async def start_due_handovers(svc: Services, horizon_minutes: int = 60) -> list[dict[str, Any]]:
    from hr_agent.handover.service import start_handover

    started: list[dict[str, Any]] = []
    for handoff in upcoming_handoffs(svc, horizon_minutes):
        ctx = shift_context_id(handoff)
        request = AgentRequest(
            meta=RequestMeta(
                context_id=ctx,
                token="internal:hr",
                path=[],
                deadline=svc.clock() + timedelta(seconds=30),
                idempotency_key=f"{ctx}:start_handover",
                requester="hr",
            ),
            skill="start_handover",
            input={
                "owner_id": handoff.from_member,
                "successor_id": handoff.to_member,
                "area": handoff.area,
                "reason": "shift",
            },
        )
        result = await start_handover(svc, request)
        output = result.output or {}
        started.append(
            {
                "area": handoff.area,
                "at": handoff.at.isoformat(),
                "handover_id": output.get("handover_id"),
                "status": output.get("status"),
            }
        )
        svc.audit.record(ctx, "timer.shift_handover", started[-1])
    return started


async def run_forever(interval_seconds: int = 60) -> None:
    while True:
        try:
            await start_due_handovers(get_services())
        except Exception:  # noqa: BLE001 - 타이머는 어떤 오류에도 멈추지 않는다
            log.exception("교대 점검 실패")
        await asyncio.sleep(interval_seconds)
