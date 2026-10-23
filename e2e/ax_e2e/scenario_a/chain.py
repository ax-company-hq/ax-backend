"""연결 점수 (총괄 경로): 장애 이벤트 하나를 총괄 `POST /api/events`에 넣고
12단계가 이어지는지 본다.

혼합 모드: Agent Card에 skill이 있는 부서는 실제 앱(ASGI)으로, 카드가 비어 있는 부서(지금 infra)는
총괄 목업의 가짜 부서가 대신한다. 가짜 부서가 맡은 단계는 pass가 아니라 absent(pending)다.
단계마다 확인:
 1. 총괄이 call_agent로 그 skill을 보냈다 (호출 기록)
 2. 사건 단계가 completed
 3. 요청의 context_id가 사건의 `ctx_inc_` 하나 (모든 호출이 같다)
 4. 공통 로그에 시간순으로 남았다 (기록기가 없으면 이 검사만 pending → 단계는 pending)
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

from ax_e2e.scenario_a import harness, paths
from ax_e2e.scenario_a.result import Outcome
from ax_e2e.scenario_a.stages import INCIDENT, Stage

POLL_TIMEOUT = 60.0


def real_depts() -> list[str]:
    """카드에 skill이 하나라도 있는 부서 = 실제 앱으로 부른다."""
    return sorted(d for d in paths.cards() if paths.card_skills(d))


class CallRecorder:
    """총괄의 call_agent 호출을 기록하며 그대로 전달한다."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, target, req, **kw):
        self.calls.append(
            {
                "dept": target,
                "skill": req.skill,
                "context_id": req.meta.context_id,
                "path": list(req.meta.path),
                "at": datetime.now(UTC).isoformat(),
            }
        )
        return await self.inner(target, req, **kw)


def incident_event() -> dict[str, Any]:
    from ax_contracts.schemas import EventEnvelope, EventType

    env = EventEnvelope(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        type=EventType.SHOP_DEGRADED,
        occurred_at=datetime.now(UTC),
        context_id=None,
        source="shop-api",
        idempotency_key=f"e2e:{uuid.uuid4().hex[:8]}",
        data={"component": "payment", "metric_code": "error_rate_5xx", "value": 0.42},
    )
    return env.model_dump(mode="json")


def read_log(ctx: str) -> list | None:
    """공통 로그에서 사건 기록을 읽는다. 저장소가 없거나 비어 있으면 None."""
    from ax_agent_template import common_log
    from ax_contracts.data_access.common_log import CommonLogQuery

    store = common_log.get_store()
    if store is None:
        return None
    records = store.query(CommonLogQuery(context_ids=[ctx], limit=5000)).records
    return records or None


def log_times(
    stages: list[Stage], records: list | None
) -> dict[str, tuple[datetime, datetime] | None]:
    """단계 → (요청 받은 시각, 응답 보낸 시각).

    같은 skill이 여러 단계면 plan 순서대로 n번째 기록에 맞춘다.
    """
    if records is None:
        return {}
    out: dict[str, tuple[datetime, datetime] | None] = {}
    inc = [s for s in stages if s.path == INCIDENT]
    for stage in inc:
        same = [s.key for s in inc if s.skill == stage.skill and s.dept == stage.dept]
        n = same.index(stage.key)
        recv = sorted(
            r.occurred_at
            for r in records
            if r.kind == "request_received" and r.skill == stage.skill and r.source == stage.dept
        )
        sent = sorted(
            r.occurred_at
            for r in records
            if r.kind == "result_sent" and r.skill == stage.skill and r.source == stage.dept
        )
        out[stage.key] = (recv[n], sent[n]) if n < len(recv) and n < len(sent) else None
    return out


def log_check(stage: Stage, records: list | None, times: dict) -> str:
    """pass | fail | pending. 그 단계의 요청·응답이 기록돼 있고 선행 단계 응답 뒤에 시작했는지."""
    if records is None:
        return "pending"
    mine = times.get(stage.key)
    if mine is None:
        return "fail"
    for dep in stage.after:
        theirs = times.get(dep)
        if theirs is not None and mine[0] < theirs[1]:
            return "fail"
    return "pass"


def judge_steps(
    stages: list[Stage], view: dict, calls: list[dict], records: list | None
) -> dict[str, Outcome]:
    ctx = view["context_id"]
    by_step = {s["step_id"]: s for s in view["steps"]}
    called = {(c["skill"], c["dept"]) for c in calls}
    ctx_ok = ctx.startswith("ctx_inc_") and all(c["context_id"] == ctx for c in calls)
    real = real_depts()
    times = log_times(stages, records)
    out: dict[str, Outcome] = {}
    for stage in (s for s in stages if s.path == INCIDENT):
        run = by_step.get(stage.key)
        if run is None:
            out[stage.key] = Outcome("fail", "사건에 단계가 없음")
            continue
        if stage.dept not in real or stage.skill not in paths.card_skills(stage.dept):
            out[stage.key] = Outcome(
                "absent", f"{stage.dept} Agent Card에 {stage.skill} 없음 (가짜 부서가 대신함)"
            )
            continue
        note = run.get("note") or ""
        if run["status"] == "skipped":
            blocked = note.startswith("선행 단계 미완료")
            by = [x.strip() for x in note.partition("(")[2].rstrip(")").split(",") if x.strip()]
            out[stage.key] = (
                Outcome("blocked", note, blocked_by=by) if blocked else Outcome("fail", note)
            )
            continue
        checks = {
            "call_agent": "pass" if (stage.skill, run.get("dept")) in called else "fail",
            "completed": "pass" if run["status"] == "completed" else "fail",
            "context_id": "pass" if ctx_ok else "fail",
        }
        # 공통 로그: 단계가 시작된 부서가 실제 앱이므로 request_received가 있어야 한다
        checks["common_log"] = log_check(stage, records, times)
        fails = [k for k, v in checks.items() if v == "fail"]
        if fails:
            detail = run["status"] + (f": {note}" if note else "")
            out[stage.key] = Outcome("fail", f"{', '.join(fails)} 실패 ({detail})", checks=checks)
        elif "pending" in checks.values():
            out[stage.key] = Outcome("pending", "공통 로그 기록기 없음", checks=checks)
        else:
            out[stage.key] = Outcome("pass", checks=checks)
    return out


def run_chain(stages: list[Stage], apps: harness.Apps):
    """총괄 앱을 띄워 사건 하나를 끝까지 돌린다. (단계 결과, 사건 보기, 호출 기록)를 돌려준다."""
    from fastapi.testclient import TestClient

    import ax_orchestrator.hybrid as hybrid
    from ax_orchestrator import main as orch

    recorder = CallRecorder(hybrid.call_agent)
    hybrid.call_agent = recorder  # type: ignore[assignment]
    import os

    os.environ["ORCHESTRATOR_REAL_DEPTS"] = ",".join(d for d in real_depts() if d in orch.DEPTS)
    orch.state.__init__()  # 환경변수를 다시 읽는다
    view: dict | None = None
    with TestClient(orch.app) as client:
        resp = client.post("/api/events", json=incident_event())
        data = resp.json().get("data") or {}
        ctx = data.get("context_id")
        if resp.status_code != 200 or not ctx:
            return (
                {
                    s.key: Outcome("fail", f"/api/events {resp.status_code} {data}")
                    for s in stages
                    if s.path == INCIDENT
                },
                None,
                recorder.calls,
            )
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            view = client.get(f"/api/incidents/{ctx}").json()["data"]
            if view["status"] != "open":
                break
            time.sleep(0.2)
        assert view is not None
        if view["status"] == "open":
            return (
                {
                    s.key: Outcome("fail", "사건이 시간 안에 끝나지 않음")
                    for s in stages
                    if s.path == INCIDENT
                },
                view,
                recorder.calls,
            )
        records = read_log(ctx)
        return judge_steps(stages, view, recorder.calls, records), view, recorder.calls
