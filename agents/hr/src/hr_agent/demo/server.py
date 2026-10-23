"""데모 서버: 화면(`page.html`)과 API. 개발 전용이며 목 모드(`AGENT_TEMPLATE_MOCK=1`)로 돈다.

- `/api/state`: 지금 상태(카드·보류 큐·감사 로그 등)
- `/api/call`: skill 하나를 hr의 `/a2a`로 보낸다(탭의 입력 양식이 쓴다)
- `/api/scenarios`·`/api/run`·`/api/run-all`: 샘플 시나리오 실행(시나리오마다 새 상태로 돌린다)
- `/api/reset`·`/api/clock/advance`·`/api/holds/{id}/decide`: 데모 조작
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

os.environ.setdefault("AGENT_TEMPLATE_MOCK", "1")
os.environ.setdefault("HR_MOCK", "1")

from hr_agent.demo import runner  # noqa: E402  (목 모드 환경변수를 먼저 정한다)
from hr_agent.demo.snapshot import snapshot  # noqa: E402
from hr_agent.services import Services, set_services  # noqa: E402

PAGE = Path(__file__).resolve().parent / "page.html"

demo_app = FastAPI(title="hr demo", docs_url=None, redoc_url=None)
_lock = asyncio.Lock()  # 데모 호출은 한 번에 하나씩 처리한다(전역 상태를 공유하므로)
_state: dict[str, Services] = {}


def interactive() -> Services:
    """탭에서 직접 조작하는 상태. 처음 쓸 때 만들고 `/api/reset`으로 새로 만든다."""
    if "svc" not in _state:
        _state["svc"] = runner.new_services()
    set_services(_state["svc"])
    return _state["svc"]


class CallRequest(BaseModel):
    skill: str
    requester: str
    input: dict[str, Any] = {}
    untrusted: dict[str, Any] = {}
    context_id: str | None = None
    idempotency_key: str | None = None


class RunRequest(BaseModel):
    name: str
    repeat: int = 2


class DecideRequest(BaseModel):
    approved: bool


class AdvanceRequest(BaseModel):
    minutes: int


@demo_app.get("/", response_class=HTMLResponse)
async def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@demo_app.get("/api/state")
async def state() -> dict[str, Any]:
    async with _lock:
        return await snapshot(interactive())


@demo_app.post("/api/call")
async def call(req: CallRequest) -> dict[str, Any]:
    async with _lock:
        svc = interactive()
        counter = len(svc.store.all("SELECT id FROM audit_logs")) + 1
        sent = await runner.call_skill(
            svc,
            skill=req.skill,
            requester=req.requester,
            input=req.input,
            untrusted=req.untrusted,
            context_id=req.context_id,
            key=req.idempotency_key or f"demo:{req.skill}:{counter}",
        )
        return {**sent, "state": await snapshot(svc)}


@demo_app.post("/api/reset")
async def reset() -> dict[str, Any]:
    async with _lock:
        old = _state.pop("svc", None)
        if old:
            old.store.close()
        return await snapshot(interactive())


@demo_app.post("/api/clock/advance")
async def advance(req: AdvanceRequest) -> dict[str, Any]:
    async with _lock:
        svc = interactive()
        svc.clock.advance(minutes=max(0, req.minutes))  # type: ignore[attr-defined]
        return await snapshot(svc)


class TimerRequest(BaseModel):
    horizon_minutes: int = 60


@demo_app.post("/api/timer/run")
async def run_timer(req: TimerRequest) -> dict[str, Any]:
    """교대 시각 점검을 한 번 돌린다(운영에서는 `HR_TIMER=1`로 주기 실행)."""
    from hr_agent.roster.timer import start_due_handovers

    async with _lock:
        svc = interactive()
        set_services(svc)
        started = await start_due_handovers(svc, max(1, req.horizon_minutes))
        return {"started": started, "state": await snapshot(svc)}


@demo_app.get("/api/audit")
async def audit_contexts() -> dict[str, Any]:
    async with _lock:
        return {"contexts": interactive().audit.contexts()}


@demo_app.get("/api/audit/{context_id}")
async def audit_timeline(context_id: str) -> dict[str, Any]:
    async with _lock:
        svc = interactive()
        return {"context_id": context_id, "timeline": svc.audit.timeline(context_id)}


@demo_app.post("/api/holds/{review_id}/decide")
async def decide_hold(review_id: str, req: DecideRequest) -> dict[str, Any]:
    async with _lock:
        svc = interactive()
        try:
            svc.gate.decide_hold(review_id, req.approved, by="demo")  # type: ignore[attr-defined]
        except KeyError as exc:
            raise HTTPException(404, f"없는 보류 건: {review_id}") from exc
        return await snapshot(svc)


@demo_app.get("/api/scenarios")
async def scenarios() -> list[dict[str, Any]]:
    return [
        {
            "name": s["name"],
            "title": s["title"],
            "description": s.get("description", ""),
            "steps": len(s["steps"]),
        }
        for s in runner.load_scenarios().values()
    ]


@demo_app.post("/api/run")
async def run(req: RunRequest) -> dict[str, Any]:
    items = runner.load_scenarios()
    if req.name not in items:
        raise HTTPException(404, f"없는 시나리오: {req.name}")
    async with _lock:
        out = await runner.run_scenario(req.name, items, max(1, min(req.repeat, 5)))
        set_services(_state.get("svc"))
        return out


@demo_app.post("/api/run-all")
async def run_all() -> dict[str, Any]:
    items = runner.load_scenarios()
    async with _lock:
        runs = [await runner.run_scenario(name, items) for name in items]
        set_services(_state.get("svc"))
    return {
        "passed": sum(1 for r in runs if r["passed"] and r["deterministic"]),
        "total": len(runs),
        "runs": runs,
    }
