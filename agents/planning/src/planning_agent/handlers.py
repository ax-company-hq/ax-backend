"""planning A2A skill 핸들러 (시나리오 A 필수 3개).

- 입력은 계약 모델(`contracts/src/ax_contracts/schemas/planning.py`)로 검증하고, 출력도 계약 모델로
  만든 뒤 `output`에 넣는다. 형식을 새로 정의하지 않는다.
- `request.untrusted`는 읽지 않는다 (지시로 해석하지 않는다).
- 원인 판정 문장을 만들지 않는다 (원인 판정은 infra 몫). 근거는 `evidence`에 붙인다.
- 상태를 바꾸는 `link_incident`는 서비스 안에서 `execute()`를 거친다. 관문이 거부하면 실행하지
  않고 `failed`(outcome `gate_rejected`)로 돌려준다.
- 사람 버튼(기획서 확정·PR 머지·승급 승인)은 A2A로 받지 않는다. REST(`api.py`)로만 받는다.
"""

from __future__ import annotations

from pydantic import ValidationError

from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from ax_contracts.schemas.planning import (
    FindRelatedPrsInput,
    FindRelatedPrsOutput,
    LinkIncidentInput,
    LinkIncidentOutput,
    RelatedPr,
    VerifyRollbackTargetInput,
    VerifyRollbackTargetOutput,
)
from planning_agent.domain.models import PR
from planning_agent.domain.rules import ROLLBACK_LOOKBACK
from planning_agent.domain.service import GateRefused
from planning_agent.runtime import get_service


def _related(rows: list[tuple[PR, str | None, str | None]]) -> list[RelatedPr]:
    return [
        RelatedPr(
            pr_number=pr.number,
            title=pr.title,
            merged_at=pr.merged_at,
            spec_id=spec_id,
            para_id=para_id,
            changed_paths=list(pr.changed_paths),
        )
        for pr, spec_id, para_id in rows
    ]


def _refs(rows: list[RelatedPr]) -> list[str]:
    refs: list[str] = []
    for pr in rows:
        refs.append(f"pr:{pr.pr_number}")
        if pr.spec_id and pr.para_id:
            refs.append(f"para:{pr.spec_id}:{pr.para_id}")
    return refs


def _invalid(request: AgentRequest, exc: ValidationError) -> AgentResult:
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.FAILED,
        outcome="invalid_input",
        output={"message": str(exc)},
    )


def _requester(request: AgentRequest) -> str:
    return request.meta.requester or "orchestrator"


async def find_related_prs(request: AgentRequest) -> AgentResult:
    """구간 안 머지 PR 후보 (read). 원인은 판정하지 않는다."""
    try:
        inp = FindRelatedPrsInput.model_validate(request.input)
    except ValidationError as exc:
        return _invalid(request, exc)
    rows = await get_service().find_related_prs(
        context_id=request.meta.context_id,
        requester=_requester(request),
        window_from=inp.window_from,
        window_to=inp.window_to,
        component=inp.component,
    )
    candidates = _related(rows)
    comp = f" AND changed_paths 접두사 '{inp.component}'" if inp.component else ""
    summary = (
        f"구간 안 머지 PR {len(candidates)}건 (시간 관계만, 원인 판정은 하지 않음)"
        if candidates
        else "확인할 수 없음: 구간 안 머지 PR 없음"
    )
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        output=FindRelatedPrsOutput(candidates=candidates).model_dump(mode="json"),
        evidence=Evidence(
            queries=[
                f"prs WHERE merged AND merged_at BETWEEN '{inp.window_from.isoformat()}' "
                f"AND '{inp.window_to.isoformat()}'{comp} ORDER BY merged_at DESC"
            ],
            data_summary=summary,
            ref_ids=_refs(candidates),
        ),
    )


async def link_incident(request: AgentRequest) -> AgentResult:
    """사건을 PR·문단에 연결하고 재발 방지 요청을 등록한다 (A, execute 경유)."""
    try:
        inp = LinkIncidentInput.model_validate(request.input)
    except ValidationError as exc:
        return _invalid(request, exc)
    if not request.meta.idempotency_key:
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.FAILED,
            outcome="missing_idempotency_key",
            output={"message": "상태 변경 skill에는 idempotency_key가 필요하다"},
        )
    try:
        result = await get_service().link_incident(
            context_id=request.meta.context_id,
            requester=_requester(request),
            incident_context_id=inp.incident_context_id,
            pr_numbers=inp.pr_numbers,
            follow_up_title=inp.follow_up_title,
            idempotency_key=request.meta.idempotency_key,
        )
    except GateRefused as exc:
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.FAILED,
            outcome="gate_rejected",
            output={"message": f"실행 관문이 {exc.action}을 허락하지 않아 실행하지 않았다"},
            evidence=Evidence(
                data_summary=f"execute({exc.action}) → {exc.decision.status}",
                ref_ids=[exc.decision.execution_id],
            ),
        )
    out = LinkIncidentOutput(
        linked_paras=result.output["linked_paras"],
        follow_up_request_id=result.output["follow_up_request_id"],
    )
    missing = result.output.get("missing_pr_numbers") or []
    summary = f"문단 {len(out.linked_paras)}개에 사건 {inp.incident_context_id} 표시"
    if missing:
        summary += f" · 확인할 수 없음: PR {', '.join(f'#{n}' for n in missing)}"
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=result.outcome,
        output=out.model_dump(mode="json"),
        evidence=Evidence(
            queries=[f"prs WHERE number IN {sorted(set(inp.pr_numbers))}"],
            data_summary=summary,
            ref_ids=[f"pr:{n}" for n in inp.pr_numbers if n not in missing]
            + [f"para:{p}" for p in out.linked_paras]
            + ([f"req:{out.follow_up_request_id}"] if out.follow_up_request_id else []),
        ),
    )


async def verify_rollback_target(request: AgentRequest) -> AgentResult:
    """infra rollback_deployment(B)의 교차검증 (read). 거절도 completed + rejected_by_review."""
    try:
        inp = VerifyRollbackTargetInput.model_validate(request.input)
    except ValidationError as exc:
        return _invalid(request, exc)
    approved, reason, rows = await get_service().verify_rollback_target(
        context_id=request.meta.context_id,
        requester=_requester(request),
        service=inp.service,
        target_version=inp.target_version,
        deployed_at=inp.deployed_at,
        incident_opened_at=inp.incident_opened_at,
    )
    related = _related(rows)
    minutes = int(ROLLBACK_LOOKBACK.total_seconds() // 60)
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=None if approved else "rejected_by_review",
        output=VerifyRollbackTargetOutput(
            approved=approved, reason_code=reason, related_prs=related
        ).model_dump(mode="json"),
        evidence=Evidence(
            queries=[
                f"deployed_at({inp.deployed_at.isoformat()}) <= "
                f"incident_opened_at({inp.incident_opened_at.isoformat()})",
                f"prs WHERE merged_at BETWEEN deployed_at - {minutes}m AND deployed_at",
            ],
            data_summary=(
                f"{inp.service} {inp.target_version}: {reason}, 구간 안 머지 PR {len(related)}건"
            ),
            ref_ids=_refs(related),
        ),
    )


HANDLERS = {
    "find_related_prs": find_related_prs,
    "link_incident": link_incident,
    "verify_rollback_target": verify_rollback_target,
}
