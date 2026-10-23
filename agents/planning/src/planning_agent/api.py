"""화면용 REST (`/api/planning/*`). 성공 `{data, meta}`, 실패 `{error: {code, message}}`.

- `GET` 목록·상세는 상태를 읽기만 한다.
- `POST /actions`는 프로토타입 Action(`{"type": "confirmSpec", "spec": "SPEC-7"}`)을 그대로 받는다
  (`confirm_spec`처럼 snake_case도 받는다). 사람 버튼(확정·머지·승급 승인)은 여기로만 받는다.
- `GET /events`는 SSE(`text/event-stream`). 연결하면 현재 상태를 한 번 보내고, 상태가 바뀔 때마다
  `event: state` + 전체 상태 JSON을 보낸다. 15초마다 주석 줄로 연결을 유지한다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from planning_agent.domain.metrics import compute_metrics
from planning_agent.domain.models import dump
from planning_agent.domain.projects import project_detail, project_summary, sorted_projects
from planning_agent.domain.rules import AUTO_LEVELS, PROMOTE_AFTER, auto_limit
from planning_agent.domain.service import ActionError, GateRefused, PlanningService
from planning_agent.domain.trace import code_of, trace_of, trace_text

KEEPALIVE_SECONDS = 15.0


def _ok(data: Any, svc: PlanningService, **meta: Any) -> dict:
    return {"data": data, "meta": {"tenant_id": svc.tenant_id, "version": svc.version, **meta}}


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _list(items: list, svc: PlanningService) -> dict:
    data = [dump(x) for x in items]
    return _ok(data, svc, count=len(data))


def create_api_router(get_service: Callable[[], PlanningService]) -> APIRouter:
    router = APIRouter(prefix="/api/planning", tags=["planning"])

    @router.get("/state")
    def get_state() -> dict:
        svc = get_service()
        return _ok(svc.snapshot(), svc)

    @router.get("/requests")
    def list_requests(state: str | None = None) -> dict:
        svc = get_service()
        rows = [r for r in svc.state.requests if state is None or r.state == state]
        return _list(rows, svc)

    @router.get("/specs")
    def list_specs() -> dict:
        svc = get_service()
        return _list(svc.state.specs, svc)

    @router.get("/tasks")
    def list_tasks() -> dict:
        svc = get_service()
        return _list(svc.state.tasks, svc)

    @router.get("/pull-requests")
    def list_pull_requests() -> dict:
        svc = get_service()
        return _list(svc.state.prs, svc)

    @router.get("/threads")
    def list_threads() -> dict:
        svc = get_service()
        return _list(svc.state.threads, svc)

    @router.get("/decisions")
    def list_decisions() -> dict:
        svc = get_service()
        return _list(svc.state.decisions, svc)

    @router.get("/incident-links")
    def list_incident_links() -> dict:
        svc = get_service()
        return _list(svc.state.incidents, svc)

    @router.get("/docs")
    def list_docs() -> dict:
        svc = get_service()
        return _list(svc.state.docs, svc)

    @router.get("/metrics")
    def get_metrics() -> dict:
        svc = get_service()
        return _ok(compute_metrics(svc.state), svc)

    @router.get("/projects")
    def list_projects() -> dict:
        svc = get_service()
        rows = [project_summary(svc.state, r) for r in sorted_projects(svc.state)]
        return _ok(rows, svc, count=len(rows))

    @router.get("/projects/{project_id}", response_model=None)
    def get_project(project_id: str) -> dict | JSONResponse:
        svc = get_service()
        r = next((x for x in sorted_projects(svc.state) if x.id == project_id), None)
        if r is None:
            return _err(404, "NOT_FOUND", f"프로젝트 {project_id}가 없다")
        return _ok(project_detail(svc.state, r), svc)

    @router.get("/trace/{pr_id}", response_model=None)
    def get_trace(pr_id: str) -> dict | JSONResponse:
        """`28`, `PR-28`, `PR #28`(URL 인코딩 `PR%20%2328`) 모두 받는다."""
        svc = get_service()
        digits = "".join(ch for ch in pr_id if ch.isdigit())
        pr = next((p for p in svc.state.prs if digits and p.number == int(digits)), None)
        if pr is None:
            return _err(404, "NOT_FOUND", f"PR {pr_id}가 없다")
        steps = trace_of(svc.state, pr.id)
        return _ok(
            {
                "pr": pr.id,
                "code": [c.model_dump() for c in code_of(svc.state, pr)],
                "steps": [x.model_dump() for x in steps],
                "text": trace_text(steps),
            },
            svc,
        )

    @router.get("/review-queue")
    def list_review_queue(state: str | None = None) -> dict:
        """자동 확정·자동 머지를 멈춘 건 (답 대기·교차검증 대기·사람 확인).

        판단은 `decide_with`의 기존 사람 버튼(`POST /actions`)으로 한다.
        실제 교차검증자 연결·학습은 safety 협의 뒤 (#166 3번).
        """
        svc = get_service()
        rows: list[dict] = []
        for sp in svc.state.specs:
            if sp.status != "확정" and sp.auto_review is not None:
                rows.append(
                    {
                        "kind": "spec",
                        "id": sp.id,
                        "title": sp.title,
                        "action": "confirm_spec",
                        **dump(sp.auto_review),
                        "decide_with": {"type": "confirmSpec", "spec": sp.id},
                    }
                )
        for pr in svc.state.prs:
            if not pr.merged and pr.auto_review is not None:
                rows.append(
                    {
                        "kind": "pr",
                        "id": pr.id,
                        "title": pr.title,
                        "action": "merge_pr",
                        **dump(pr.auto_review),
                        "decide_with": {"type": "mergePr", "pr": pr.id},
                    }
                )
        if state is not None:
            rows = [r for r in rows if r["state"] == state]
        return _ok(rows, svc, count=len(rows))

    @router.get("/auto-policy")
    def get_auto_policy() -> dict:
        svc = get_service()
        pol = svc.state.auto_policy
        return _ok(
            {
                **dump(pol),
                "limit": auto_limit(pol),
                "levels": [lv.model_dump() for lv in AUTO_LEVELS],
                "promote_after": PROMOTE_AFTER,
                "linked_incidents": sum(1 for h in pol.history if h.result == "장애 연결"),
            },
            svc,
        )

    @router.post("/actions", response_model=None)
    async def post_action(request: Request) -> dict | JSONResponse:
        svc = get_service()
        try:
            body = await request.json()
        except ValueError:
            return _err(400, "INVALID_ACTION", "JSON이 아니다")
        try:
            result = await svc.dispatch(body)
        except ActionError as exc:
            status = 403 if exc.code == "SIMULATOR_DISABLED" else 400
            return _err(status, exc.code, exc.message)
        except GateRefused as exc:
            return _err(
                409,
                "GATE_REJECTED",
                f"실행 관문이 {exc.action}을 허락하지 않아 실행하지 않았다 ({exc.decision.status})",
            )
        return _ok(
            svc.snapshot(),
            svc,
            changed=result.changed,
            outcome=result.outcome,
            result=result.output,
        )

    @router.post("/requests/{request_id}/reject", response_model=None)
    async def post_reject_request(request_id: str, request: Request) -> dict | JSONResponse:
        """개선 초안 기각. 본문 `{decision_reason, alternatives_considered}` 둘 다 필수.

        `POST /actions` + `{"type": "rejectDraft", ...}`와 같은 길이다. 이유·대안이 없으면
        hr에 `decision_rejected`를 보내지 않고 400 `MISSING_DECISION_REASON`.
        """
        svc = get_service()
        try:
            body = await request.json()
        except ValueError:
            return _err(400, "INVALID_ACTION", "JSON이 아니다")
        if not isinstance(body, dict):
            return _err(400, "INVALID_ACTION", "본문은 객체여야 한다")
        if not any(r.id == request_id for r in svc.state.requests):
            return _err(404, "NOT_FOUND", f"요청 {request_id}가 없다")
        try:
            result = await svc.dispatch({**body, "type": "reject_draft", "request": request_id})
        except ActionError as exc:
            return _err(400, exc.code, exc.message)
        except GateRefused as exc:
            return _err(
                409,
                "GATE_REJECTED",
                f"실행 관문이 {exc.action}을 허락하지 않아 실행하지 않았다 ({exc.decision.status})",
            )
        return _ok(
            svc.snapshot(),
            svc,
            changed=result.changed,
            outcome=result.outcome,
            result=result.output,
        )

    @router.post("/reset")
    def post_reset() -> dict:
        svc = get_service()
        svc.reset()
        return _ok(svc.snapshot(), svc)

    @router.get("/events")
    async def events(max_events: int | None = None) -> StreamingResponse:
        """`max_events`는 테스트·점검용: 그만큼 보내면 스트림을 닫는다."""
        svc = get_service()

        def frame() -> str:
            data = json.dumps(svc.snapshot(), ensure_ascii=False)
            return f"id: {svc.version}\nevent: state\ndata: {data}\n\n"

        async def stream():
            queue = svc.subscribe()
            sent = 0
            try:
                yield frame()
                sent += 1
                while max_events is None or sent < max_events:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    while not queue.empty():  # 몰려온 변경은 마지막 상태 한 번으로
                        queue.get_nowait()
                    yield frame()
                    sent += 1
            finally:
                svc.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
