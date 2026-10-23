"""단독 점수: 단계마다 그 부서를 고정 목 입력으로 직접 호출한다.

입력은 plan의 `build`가 만든다 (총괄이 실제로 보내는 입력과 같은 형식). 앞 단계 결과가 필요한 단계는
앞 단계 단독 호출이 completed면 그 출력을, 아니면 `CANNED_OUTPUTS`의 고정 값을 쓴다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ax_e2e.scenario_a import harness, paths
from ax_e2e.scenario_a.result import Outcome
from ax_e2e.scenario_a.stages import CUSTOMER, INCIDENT, Stage

UNIT_CTX = "ctx_inc_e2e_unit0001"
CANNED_OUTPUTS: dict[str, dict[str, Any]] = {
    "diagnose": {
        "suspects": [{"service": "payment-api", "version": "v2.4.1", "previous_version": "v2.4.0"}]
    },
    "related_prs": {"candidates": [{"pr_number": 101}]},
    "notice": {"draft_id": "draft_e2e_0001"},
    "recovered_draft": {"draft_id": "draft_e2e_0002"},
}
OPENED_AT = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)
GOOD_STATUS = {"completed"}


def classify(stage: Stage, apps: harness.Apps, body: dict, ctx: str) -> Outcome:
    """부서 응답 하나가 통과 조건을 채우는지."""
    if body.get("http") != 200:
        return Outcome("fail", f"http {body.get('http')} {body.get('outcome') or ''}".strip())
    status = body.get("status")
    meta_ctx = (body.get("meta") or {}).get("context_id")
    if status not in GOOD_STATUS:
        return Outcome("fail", f"status={status} outcome={body.get('outcome')}")
    if meta_ctx != ctx:
        return Outcome("fail", f"context_id 불일치({meta_ctx})")
    return Outcome("pass")


def absent(stage: Stage, apps: harness.Apps) -> Outcome | None:
    """skill이 부서 Agent Card에 아직 없으면 pending."""
    if not stage.skill or stage.skill not in paths.card_skills(stage.dept):
        return Outcome("absent", f"{stage.dept} Agent Card에 {stage.skill} 없음")
    if stage.dept in apps.errors:
        return Outcome("fail", apps.errors[stage.dept])
    return None


async def run_incident_units(
    stages: list[Stage], apps: harness.Apps
) -> tuple[dict[str, Outcome], dict[str, dict]]:
    """(단계 결과, 단계별 출력). 출력은 뒤 단계 입력과 고객 경로 단독 호출이 쓴다."""
    from ax_orchestrator.models import Incident
    from ax_orchestrator.plans.scenario_a import scenario_a

    inc = Incident(UNIT_CTX, "scenario_a", "payment 장애", "payment", opened_at=OPENED_AT)
    steps = {s.id: s for s in scenario_a().steps}
    out: dict[str, Outcome] = {}
    for stage in (s for s in stages if s.path == INCIDENT):
        skip = absent(stage, apps)
        if skip:
            out[stage.key] = skip
            inc.outputs[stage.key] = CANNED_OUTPUTS.get(stage.key, {})
            continue
        step = steps[stage.key]
        data = step.build(inc) if step.kind == "call" else _record_input(inc)
        req = harness.build_request(
            stage.skill,
            data,
            requester="orchestrator",
            context_id=UNIT_CTX,
            idempotency_key=f"{UNIT_CTX}:{stage.skill}:{stage.key}",
        )
        body = await harness.call_dept(stage.dept, req)
        out[stage.key] = classify(stage, apps, body, UNIT_CTX)
        real = body.get("output") if out[stage.key].state == "pass" else None
        inc.outputs[stage.key] = real or CANNED_OUTPUTS.get(stage.key, {})
    return out, inc.outputs


def _record_input(inc) -> dict[str, Any]:
    return {
        "context_id": inc.context_id,
        "kind": "incident",
        "summary": f"{inc.title} — e2e 단독 호출",
        "decision_reason": "e2e 고정 입력",
        "alternatives_considered": [],
        "source_refs": ["step:diagnose"],
        "author": "orchestrator",
        "area": "infra",
    }


__all__ = ["run_incident_units", "classify", "absent", "CUSTOMER"]
