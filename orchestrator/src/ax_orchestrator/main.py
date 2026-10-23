"""ax-orchestrator 서버 진입점.

목 모드(`ORCHESTRATOR_MOCK=1`)에서는 가짜 부서(`mock/fake_departments.py`)로 시나리오 A를
끝까지 돌리고, `/demo` 목업 화면으로 사건 흐름을 보여 준다. 실제 부서 연동은 3단계에서 붙인다.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ax_agent_template.config import set_agent_name
from ax_contracts.schemas import EventEnvelope, EventType
from ax_orchestrator.engine import Engine
from ax_orchestrator.hybrid import HybridDepartments, real_depts_from_env
from ax_orchestrator.manager import IncidentManager
from ax_orchestrator.mock.fake_departments import FakeDepartments
from ax_orchestrator.models import Incident, now
from ax_orchestrator.plans.scenario_a import scenario_a
from ax_orchestrator.registry import Registry
from ax_orchestrator.usage import BUCKET_SECONDS, WINDOW_SECONDS, RecordingCaller, UsageRecorder

STATIC = Path(__file__).parent / "static"
DEPTS = ["infra", "data", "planning", "marketing", "support", "finance", "hr"]


class DemoRequest(BaseModel):
    """목업 화면의 장애 주입 옵션. 각 옵션은 가짜 부서의 동작을 바꾼다."""

    hold_rollback: bool = False  # 롤백이 C 등급 보류가 됨(사람 승인 대기)
    fail_marketing: bool = False  # 공지 게시가 한 번 실패(재시도로 복구)
    wrong_owner: bool = False  # 공지 게시가 소관 아님으로 거절 → 이관 부서 없음 → 이관 큐로 넘김
    infra_card_empty: bool = False  # 인프라 카드 수집 실패(= 현재 dev 상태)


class Decision(BaseModel):
    approved: bool


class AppState:
    def __init__(self) -> None:
        self.mock = os.getenv("ORCHESTRATOR_MOCK") == "1"
        self.hold_timeout = float(os.getenv("ORCHESTRATOR_HOLD_TIMEOUT", "600"))
        self.latency = float(os.getenv("ORCHESTRATOR_MOCK_LATENCY", "0.7"))
        self.registry = Registry()
        self.fakes: FakeDepartments | None = None
        self.manager: IncidentManager | None = None
        self.published: list[dict[str, Any]] = []
        self.modes: dict[str, str] = {}
        self.usage = UsageRecorder()  # 사건·옵션이 바뀌어도 부서별 사용량은 이어서 쌓는다

    async def build(self, demo: DemoRequest | None = None) -> IncidentManager:
        demo = demo or DemoRequest()
        fakes = FakeDepartments(latency=self.latency)
        if demo.infra_card_empty:
            fakes.empty_cards.add("infra")
        if demo.hold_rollback:
            fakes.script("infra", "rollback_deployment", "hold")
        if demo.fail_marketing:
            fakes.script("marketing", "publish_notice", "fail")
        if demo.wrong_owner:
            fakes.script("marketing", "publish_notice", "forbidden")
        real = real_depts_from_env(DEPTS)
        if real:
            set_agent_name("orchestrator")
        caller = HybridDepartments(fakes, real)
        self.modes = caller.modes(DEPTS)
        await self.registry.load(DEPTS, caller.fetch_card)
        engine = Engine(
            self.registry,
            RecordingCaller(caller, self.usage),
            publish=self._publish,
            hold_timeout=self.hold_timeout,
            retry_delay=0.2,
        )
        self.fakes = fakes
        if self.manager is None:
            self.manager = IncidentManager(engine, scenario_a)
        else:  # 같은 사건 목록을 유지하면서 부서 동작만 교체
            self.manager.engine = engine
        return self.manager

    async def _publish(self, kind: str, inc: Incident) -> None:
        self.published.append({"type": kind, "context_id": inc.context_id})


state = AppState()
app = FastAPI(title="ax-orchestrator")


@app.get("/health")
def health() -> dict:
    return {"data": {"status": "ok"}, "meta": {}}


def _require_mock() -> None:
    if not state.mock:
        raise HTTPException(501, "실제 연동은 아직 없음 (ORCHESTRATOR_MOCK=1로 실행)")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/demo")


@app.get("/demo")
def demo_page() -> FileResponse:
    return FileResponse(STATIC / "demo.html")


@app.get("/api/registry")
async def registry() -> dict:
    _require_mock()
    if state.manager is None:
        await state.build()
    view = state.registry.view()
    for dept in view["depts"]:
        dept["mode"] = state.modes.get(dept["name"], "mock")
    return {"data": view, "meta": {"mock": True, "modes": state.modes}}


@app.get("/api/departments")
async def departments() -> dict:
    """부서별 카드·skill·사용량(총괄이 부른 호출을 센 실측)과 최근 호출. 화면의 부서 카드가 쓴다."""
    _require_mock()
    if state.manager is None:
        await state.build()
    view = state.registry.view()
    current = now()
    items: list[dict[str, Any]] = []
    for dept in view["depts"]:
        name = dept["name"]
        skills = []
        for skill in dept["skills"]:
            per_skill = state.usage.summary(name, current, skill["id"])
            skills.append(
                {
                    **skill,
                    **{k: per_skill[k] for k in ("calls", "errors", "success_rate", "avg_ms")},
                    "retries": per_skill["retries"],
                    "series": state.usage.series(name, current, skill["id"]),
                }
            )
        items.append(_dept_item(name, dept["owner"], True, None, skills, current))
    for name, reason in view["unavailable"].items():
        items.append(_dept_item(name, None, False, reason, [], current))
    items.sort(key=lambda d: DEPTS.index(d["name"]) if d["name"] in DEPTS else len(DEPTS))
    return {
        "data": items,
        "meta": {"mock": True, "window_seconds": WINDOW_SECONDS, "bucket_seconds": BUCKET_SECONDS},
    }


def _dept_item(
    name: str,
    owner: str | None,
    available: bool,
    reason: str | None,
    skills: list[dict[str, Any]],
    current: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "owner": owner,
        "mode": state.modes.get(name, "mock"),
        "available": available,
        "unavailable_reason": reason,
        "skills": skills,
        "usage": state.usage.summary(name, current),
        "series": state.usage.series(name, current),
        "recent": state.usage.recent(name),
    }


@app.get("/api/registry/real")
async def registry_real() -> dict:
    """실제 부서의 Agent Card를 HTTP로 수집해 결과를 보여 준다(읽기 전용, 사건은 열지 않음)."""
    from ax_orchestrator.real import http_card_fetcher

    real = Registry()
    await real.load(DEPTS, http_card_fetcher())
    return {"data": real.view(), "meta": {"mock": False}}


@app.get("/api/incidents")
async def incidents() -> dict:
    items = state.manager.listing() if state.manager else []
    return {"data": items, "meta": {"mock": state.mock, "published": state.published}}


@app.get("/api/incidents/{context_id}")
async def incident(context_id: str) -> JSONResponse:
    inc = state.manager.incidents.get(context_id) if state.manager else None
    if inc is None:
        return JSONResponse({"error": {"code": "NOT_FOUND", "message": "사건 없음"}}, 404)
    return JSONResponse({"data": inc.view(), "meta": {}})


@app.post("/api/demo/incidents")
async def inject(req: DemoRequest | None = None) -> dict:
    """판매 사이트 결제 장애를 주입한 것처럼 `shop.degraded` 이벤트를 넣는다(목 모드)."""
    _require_mock()
    manager = await state.build(req)
    env = EventEnvelope(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        type=EventType.SHOP_DEGRADED,
        occurred_at=datetime.now(UTC),
        context_id=None,
        source="shop-api",
        idempotency_key=f"demo:{uuid.uuid4().hex[:8]}",
        data={"component": "payment", "metric_code": "error_rate_5xx", "value": 0.42},
    )
    inc = await manager.handle(env)
    assert inc is not None
    return {"data": {"context_id": inc.context_id}, "meta": {"mock": True}}


@app.post("/api/events")
async def receive_event(env: EventEnvelope) -> dict:
    """다른 부서가 이벤트를 보내 볼 수 있는 개발용 입구(목·혼합 모드).

    실제 전달은 Redis Streams(agent-template TODO)로 바뀌며, 그때도 판단은 같은 `manager.process`다.
    """
    _require_mock()
    manager = state.manager or await state.build()
    outcome, inc = await manager.process(env)
    return {
        "data": {"outcome": outcome, "context_id": inc.context_id if inc else None},
        "meta": {"mock": True},
    }


@app.post("/api/incidents/{context_id}/holds/{step_id}")
async def decide(context_id: str, step_id: str, body: Decision) -> dict:
    """목 모드의 승인·거절 흉내. 총괄이 승인하는 것이 아니다.

    이 엔드포인트에는 사람 토큰 확인이 없다(#265). 그래서 목 모드에서만 열고,
    실제 모드의 사람 결정은 안전 관문 보류 큐 하나로만 간다.
    """
    _require_mock()
    if not (state.manager and state.manager.hold_decision(context_id, step_id, body.approved)):
        raise HTTPException(404, "대기 중인 보류가 없음")
    return {"data": {"ok": True}, "meta": {}}
