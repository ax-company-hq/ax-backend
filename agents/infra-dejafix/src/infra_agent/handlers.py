"""infra skill 핸들러 3개: diagnose_incident(read), rollback_deployment(B), verify_recovery(read).

숫자·판정은 모두 코드가 한다(LLM 없음). 결과에는 근거(evidence)를 붙이고, 모르는 값은 만들지 않는다.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ax_agent_template.config import is_mock
from ax_agent_template.execute import execute
from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    Evidence,
    ExecuteDecision,
    InputReason,
    TaskStatus,
)
from ax_contracts.schemas.infra import (
    DiagnoseIncidentInput,
    RollbackDeploymentParams,
    VerifyRecoveryInput,
    VerifyRecoveryOutput,
)
from infra_agent.deployments import Deployment
from infra_agent.diagnosis import Diagnosis, diagnose
from infra_agent.runtime import get_runtime

ACTION = "rollback_deployment"


def _failed(request: AgentRequest, outcome: str, message: str) -> AgentResult:
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.FAILED,
        outcome=outcome,
        output={"message": message},
    )


def _iso(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


# ---------- diagnose_incident ----------


def _diagnosis_evidence(diag: Diagnosis, component: str | None) -> Evidence:
    where = f"components ∋ {component}" if component else "모든 구성요소"
    if diag.window_from is None:
        query = f"deployments WHERE {where} ORDER BY deployed_at DESC LIMIT 1 (목 모드)"
    else:
        query = (
            f"deployments WHERE {where} AND deployed_at BETWEEN "
            f"{_iso(diag.window_from)} AND {_iso(diag.diagnosed_at)}"
        )
    if diag.candidates:
        summary = ", ".join(
            f"{s.service} {s.version}({_iso(s.deployed_at)}, 확신도 {s.confidence})"
            for s in diag.output.suspects
        )
    else:
        summary = "진단 구간 안에 이 구성요소를 바꾼 배포가 없음 — 원인 확인할 수 없음"
    return Evidence(
        queries=[query],
        data_summary=summary,
        collected_at=diag.diagnosed_at,
        ref_ids=[f"deploy:{d.deploy_id}" for d in diag.candidates],
    )


async def diagnose_incident(request: AgentRequest) -> AgentResult:
    try:
        inp = DiagnoseIncidentInput.model_validate(request.input)
    except ValidationError as exc:
        return _failed(request, "invalid_input", str(exc))
    rt = get_runtime()
    diag = diagnose(rt.history, inp.component, rt.clock(), mock=is_mock("infra"))
    rt.diagnoses[request.meta.context_id] = diag
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=diag.output.status,
        output=diag.output.model_dump(mode="json"),
        evidence=_diagnosis_evidence(diag, inp.component),
    )


# ---------- rollback_deployment ----------


class RollbackInput(BaseModel):
    """요청 입력. 계약 `RollbackDeploymentParams`와 같되 `to_version`은 비워도 된다
    (#239 A안: infra가 배포 이력에서 직전 정상 버전을 정한다). `incident_opened_at`이 오면
    planning 교차검증 params로 넘긴다."""

    model_config = ConfigDict(extra="ignore")

    service: str
    from_version: str
    to_version: str | None = None
    release_fault_injection: bool = True
    incident_opened_at: str | None = None


def path_params(diag: Diagnosis | None, target: Deployment, prior_rollbacks: int) -> dict[str, int]:
    """safety 기준표가 B/C를 가르는 정수 4개 (docs/notes/safety/rollback-paths.md)."""
    suspect_count = len(diag.output.suspects) if diag else 0
    confidence = diag.confidence_of(target.service, target.version) if diag else None
    return {
        "suspect_count": suspect_count,
        "low_confidence": 0 if confidence == "high" else 1,
        "irreversible_count": len(target.irreversible),
        "prior_rollbacks": prior_rollbacks,
    }


def _decision_reason(target: Deployment, to_version: str, ints: dict[str, int]) -> str:
    return (
        f"{target.service} {target.version} → {to_version} 롤백 요청. "
        f"원인 후보 {ints['suspect_count']}개, 확신도 "
        f"{'high' if ints['low_confidence'] == 0 else '낮음'}, 되돌릴 수 없는 변경 "
        f"{ints['irreversible_count']}건, 이 사건의 이전 롤백 {ints['prior_rollbacks']}회"
    )


async def rollback_deployment(request: AgentRequest) -> AgentResult:
    try:
        inp = RollbackInput.model_validate(request.input)
    except ValidationError as exc:
        return _failed(request, "invalid_input", str(exc))
    rt = get_runtime()
    context_id = request.meta.context_id
    key = f"{context_id}:{ACTION}:{inp.service}:{inp.from_version}"
    if (done := rt.rollbacks.get(key)) and done["status"] == "executed":
        return done["result"].model_copy(update={"meta": request.meta})

    target = rt.history.find(inp.service, inp.from_version)
    if target is None:
        return _failed(
            request, "invalid_input", f"배포 이력에 {inp.service} {inp.from_version}이 없다"
        )
    previous = rt.history.previous_good(target)
    if previous is None:
        return _failed(request, "invalid_input", "되돌아갈 직전 정상 배포가 없다")
    if inp.to_version is not None and inp.to_version != previous.version:
        return _failed(
            request,
            "invalid_input",
            f"to_version {inp.to_version}이 직전 정상 버전 {previous.version}과 다르다",
        )

    diag = rt.diagnoses.get(context_id)
    if diag is None:  # 같은 사건의 진단 기록이 없으면 대상 배포 기준으로 다시 진단한다
        component = target.components[0] if target.components else None
        diag = diagnose(rt.history, component, rt.clock(), mock=is_mock("infra"))
    ints = path_params(diag, target, rt.executed_rollbacks(context_id))
    params: dict[str, Any] = RollbackDeploymentParams(
        service=target.service,
        from_version=target.version,
        to_version=previous.version,
        release_fault_injection=inp.release_fault_injection,
    ).model_dump(mode="json")
    params.update(ints)
    params["deployed_at"] = _iso(target.deployed_at)
    if inp.incident_opened_at:
        params["incident_opened_at"] = inp.incident_opened_at

    evidence = Evidence(
        queries=[
            f"deployments WHERE service={target.service} AND version={target.version}",
            f"deployments WHERE service={target.service} AND deployed_at < "
            f"{_iso(target.deployed_at)} AND NOT rolled_back ORDER BY deployed_at DESC LIMIT 1",
        ],
        data_summary=_decision_reason(target, previous.version, ints),
        collected_at=rt.clock(),
        ref_ids=[f"deploy:{target.deploy_id}", f"deploy:{previous.deploy_id}"],
    )
    decision = await execute(
        ACTION, params, request.meta.model_copy(update={"idempotency_key": key}), evidence
    )
    result = await _apply(request, rt, decision, target, previous.version, params, evidence)
    rt.rollbacks[key] = {"context_id": context_id, "status": decision.status, "result": result}
    if result.status != TaskStatus.COMPLETED or decision.status != "executed":
        rt.rollbacks[key]["status"] = "not_executed"
    return result


async def _apply(
    request: AgentRequest,
    rt: Any,
    decision: ExecuteDecision,
    target: Deployment,
    to_version: str,
    params: dict[str, Any],
    evidence: Evidence,
) -> AgentResult:
    base = {
        "service": target.service,
        "from_version": target.version,
        "to_version": to_version,
        "execution_id": decision.execution_id,
        "grade": decision.grade,
        "path_params": {k: params[k] for k in _INT_KEYS},
    }
    reason = _decision_reason(target, to_version, base["path_params"])
    alternatives = ["롤백하지 않고 이관 큐로 넘김", "서비스 재시작(restart_service)"]

    if decision.status == "executed":
        output = {**base, "rolled_back_to": to_version, "path": _path_label(decision)}
        if params.get("release_fault_injection", True):
            try:
                state = await rt.shop.release_fault()
            except Exception as exc:  # 관문은 통과했지만 실제 조치가 실패했다
                return AgentResult(
                    meta=request.meta,
                    status=TaskStatus.FAILED,
                    outcome="release_failed",
                    output={**base, "message": f"장애 해제 호출 실패: {type(exc).__name__}"},
                    evidence=evidence,
                )
            output["fault_released"] = True
            output["fault_state"] = state
            output["post_check_ok"] = not state.get("active", False)
        rt.history.mark_rolled_back(target)
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.COMPLETED,
            output=output,
            evidence=evidence,
            decision_reason=reason,
            alternatives_considered=alternatives,
        )
    if decision.status == "held":
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.INPUT_REQUIRED,
            reason_code=InputReason.HUMAN_INPUT,
            outcome="held",
            output={
                **base,
                "review_id": decision.review_id,
                "hold_reason": decision.hold_reason.value if decision.hold_reason else None,
                "message": "사람 승인 대기. 승인 뒤 같은 요청을 다시 보내면 실행한다",
            },
            evidence=evidence,
            decision_reason=reason,
            alternatives_considered=alternatives,
        )
    if decision.status == "rejected" and decision.execution_id != "exe_unavailable":
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.COMPLETED,
            outcome="rejected",
            output={
                **base,
                "hold_reason": decision.hold_reason.value if decision.hold_reason else None,
            },
            evidence=evidence,
        )
    outcome = {
        "rejected": "safety_unavailable",
        "verifying": "verification_pending",
        "failed": "execute_failed",
    }[decision.status]
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.FAILED,
        outcome=outcome,
        output={**base, "message": "실행하지 않았다 (fail-closed)"},
        evidence=evidence,
    )


_INT_KEYS = ("suspect_count", "low_confidence", "irreversible_count", "prior_rollbacks")


def _path_label(decision: ExecuteDecision) -> str:
    return "human_approved" if decision.grade == "C" else "auto"


# ---------- verify_recovery ----------


def _attempts() -> int:
    return max(1, int(os.getenv("INFRA_RECOVERY_ATTEMPTS") or 5))


def _interval() -> float:
    return float(os.getenv("INFRA_RECOVERY_INTERVAL_S") or 30)


async def verify_recovery(request: AgentRequest) -> AgentResult:
    try:
        inp = VerifyRecoveryInput.model_validate(request.input)
    except ValidationError as exc:
        return _failed(request, "invalid_input", str(exc))
    rt = get_runtime()
    notes: list[str] = []
    infra_ok = False
    business_ok: bool | None = None
    attempts = _attempts()
    tried = 0
    for tried in range(1, attempts + 1):
        try:
            state = await rt.shop.fault_state()
            infra_ok = not state.get("active", False) and not state.get("degraded", False)
            infra_note = f"장애 주입 {state.get('active')}, 감시 이상 {state.get('degraded')}"
        except Exception as exc:
            infra_ok = False
            infra_note = f"판매 사이트 상태 조회 실패({type(exc).__name__})"
        now = rt.clock()
        business_ok, business_note = await rt.data.payment_metrics_ok(request.meta, inp.since, now)
        notes.append(f"{tried}회차 {_iso(now)}: 서버 {infra_note} / 업무 지표 {business_note}")
        if infra_ok and business_ok is True:
            break
        wait = _interval()
        if tried == attempts or rt.clock() + timedelta(seconds=wait) >= request.meta.deadline:
            break
        await rt.sleep(wait)
    checked_at = rt.clock()
    output = VerifyRecoveryOutput(
        recovered=infra_ok and business_ok is True,
        infra_metrics_ok=infra_ok,
        business_metrics_ok=business_ok,
        checked_at=checked_at,
    )
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome="recovered" if output.recovered else "not_recovered",
        output=output.model_dump(mode="json"),
        evidence=Evidence(
            queries=[
                "GET shop-api /api/shop/admin/faults (active·degraded)",
                f"data anomaly_detection payment_fail_rate·order_fail_count "
                f"window {_iso(inp.since)} ~ 확인 시각",
            ],
            data_summary=f"{inp.component} 복구 확인 {tried}회: " + " | ".join(notes),
            collected_at=checked_at,
        ),
    )


HANDLERS = {
    "diagnose_incident": diagnose_incident,
    "rollback_deployment": rollback_deployment,
    "verify_recovery": verify_recovery,
}
