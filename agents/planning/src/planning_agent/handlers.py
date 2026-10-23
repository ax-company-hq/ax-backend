"""planning A2A skill 핸들러 (시나리오 A 필수 3개 + hr 연동 2개 + 변경 요청 접수).

- 입력은 계약 모델(`contracts/src/ax_contracts/schemas/planning.py`)로 검증하고, 출력도 계약 모델로
  만든 뒤 `output`에 넣는다. 형식을 새로 정의하지 않는다.
- `request.untrusted`는 읽지 않는다 (지시로 해석하지 않는다).
- 원인 판정 문장을 만들지 않는다 (원인 판정은 infra 몫). 근거는 `evidence`에 붙인다.
- 상태를 바꾸는 `link_incident`·`create_ticket`은 서비스 안에서 `execute()`를 거친다. 관문이 거부하면 실행하지
  않고 `failed`(outcome `gate_rejected`)로 돌려준다.
- 사람 버튼(기획서 확정·PR 머지·승급 승인)은 A2A로 받지 않는다. REST(`api.py`)로만 받는다.
- hr 연동(`list_in_progress`·`verify_handover`, 계약 #103)은 읽기다. 담당자·영역 대응과 비교는
  `domain/handover.py`의 코드 규칙으로만 한다. 인계 내용의 좋고 나쁨은 판단하지 않는다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, TaskStatus
from ax_contracts.schemas.planning import (
    CreateTicketInput,
    CreateTicketOutput,
    FindRelatedPrsInput,
    FindRelatedPrsOutput,
    InProgressItem,
    LinkIncidentInput,
    LinkIncidentOutput,
    ListInProgressInput,
    ListInProgressOutput,
    RelatedPr,
    VerifyHandoverInput,
    VerifyHandoverOutput,
    VerifyRollbackTargetInput,
    VerifyRollbackTargetOutput,
)
from planning_agent.domain.handover import ROSTER_AREAS, Item, is_roster_area
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


async def create_ticket(request: AgentRequest) -> AgentResult:
    """변경 요청 접수 (A, execute 경유, #174). 같은 요청이면 linked_duplicate, 아니면 created.

    `request.untrusted`(고객 원문)는 읽지 않는다. `evidence` 요약만 저장한다.
    """
    try:
        inp = CreateTicketInput.model_validate(request.input)
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
        result = await get_service().create_ticket(
            context_id=request.meta.context_id,
            requester=_requester(request),
            title=inp.title,
            evidence=inp.evidence,
            source_ref=inp.source_ref,
            evidence_ids=inp.evidence_ids,
            idempotency_key=request.meta.idempotency_key,
        )
    except GateRefused as exc:
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.FAILED,
            outcome="gate_rejected",
            output={"message": f"실행 관문이 {exc.action}을 허락하지 않아 접수하지 않았다"},
            evidence=Evidence(
                data_summary=f"execute({exc.action}) → {exc.decision.status}",
                ref_ids=[exc.decision.execution_id],
            ),
        )
    o = result.output
    out = CreateTicketOutput(
        request_id=o["request_id"],
        context_id=o["context_id"],
        outcome=result.outcome,  # type: ignore[arg-type]
        duplicate_of=o["duplicate_of"],
    )
    if out.outcome == "linked_duplicate":
        summary = f"같은 요청 {out.duplicate_of}에 연결 ({out.request_id}, 사건 {out.context_id})"
    else:
        summary = f"변경 요청 {out.request_id} 접수 (사건 {out.context_id})"
    refs = [f"req:{out.request_id}", f"ctx:{out.context_id}"]
    if out.duplicate_of:
        refs.append(f"req:{out.duplicate_of}")
    if inp.source_ref:
        refs.append(f"source:{inp.source_ref}")
    refs.extend(f"evidence:{e}" for e in inp.evidence_ids)
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=out.outcome,
        output=out.model_dump(mode="json"),
        evidence=Evidence(
            queries=["requests WHERE title = :title (같은 요청 판정, source_ref는 쓰지 않음)"],
            data_summary=summary,
            ref_ids=refs,
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


def _items(rows: list[Item]) -> list[InProgressItem]:
    return [
        InProgressItem(
            item_id=i.item_id,
            kind=i.kind,  # type: ignore[arg-type]
            title=i.title,
            status=i.status,
            owner_id=i.owner_id,
            area=i.area,
            updated_at=i.updated_at,
        )
        for i in rows
    ]


def _item_refs(rows: list[Item]) -> list[str]:
    refs: list[str] = []
    for i in rows:
        refs.append(f"item:{i.item_id}")
        refs.extend(r for r in i.refs if r not in refs)
    return refs


def _where(owner_id: str, area: str | None) -> str:
    return f"owner_id = '{owner_id}'" + (f" AND area = '{area}'" if area else "")


async def list_in_progress(request: AgentRequest) -> AgentResult:
    """담당자·영역의 진행 중 배포·미완료 작업·열린 PR (read). 상태를 바꾸지 않는다."""
    try:
        inp = ListInProgressInput.model_validate(request.input)
    except ValidationError as exc:
        return _invalid(request, exc)
    rows, at = await get_service().list_in_progress(
        context_id=request.meta.context_id,
        requester=_requester(request),
        owner_id=inp.owner_id,
        area=inp.area,
    )
    kinds = {k: sum(1 for i in rows if i.kind == k) for k in ("deployment", "ticket", "pr")}
    if rows:
        summary = (
            f"진행 중 항목 {len(rows)}건 (배포 확인 전 {kinds['deployment']}, "
            f"미완료 작업 {kinds['ticket']}, 열린 PR {kinds['pr']})"
        )
    elif inp.area is not None and not is_roster_area(inp.area):
        summary = f"확인할 수 없음: 명부에 없는 영역 '{inp.area}' ({', '.join(ROSTER_AREAS)})"
    else:
        summary = f"확인할 수 없음: {inp.owner_id}의 진행 중 항목 없음"
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        output=ListInProgressOutput(items=_items(rows), collected_at=at).model_dump(mode="json"),
        evidence=Evidence(
            queries=[
                f"tasks WHERE state != '완료' AND {_where(inp.owner_id, inp.area)}",
                f"prs WHERE NOT merged AND {_where(inp.owner_id, inp.area)}",
                "prs WHERE merged AND (linked incident follow-up open OR request not closed) "
                f"AND {_where(inp.owner_id, inp.area)}",
            ],
            data_summary=summary,
            ref_ids=_item_refs(rows),
        ),
    )


class SafetyReviewInput(BaseModel):
    """safety 교차검증 호출 형식 (`safety/src/ax_safety/gate.py` `_cross_verify`, #187).

    safety는 B 액션의 검증자 skill을 계약 입력이 아니라 실행 요청 그대로 부른다:
    `{execution_id, action, requester, params, computed_amount, threshold_version}`.
    hr `confirm_handover`의 params는 `handover_id`·`owner_id`·`area`·`item_ids`(또는
    `handover_item_ids`)·`completeness_percent` 등이다. 완성도는 safety 규칙이 보므로 쓰지 않는다.
    """

    model_config = ConfigDict(extra="ignore")

    execution_id: str
    action: str
    requester: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


HANDOVER_ACTIONS = ("confirm_handover",)


def _handover_input(raw: dict[str, Any]) -> tuple[VerifyHandoverInput, str | None]:
    """계약 형식이면 그대로, safety 검토 형식이면 계약 입력으로 바꾼다. (입력, execution_id)."""
    if "execution_id" not in raw or "params" not in raw:
        return VerifyHandoverInput.model_validate(raw), None
    review = SafetyReviewInput.model_validate(raw)
    if review.action not in HANDOVER_ACTIONS:
        raise ValueError(f"verify_handover가 검증하지 않는 액션: {review.action}")
    p = review.params
    items = p.get("handover_item_ids")
    if items is None:
        items = p.get("item_ids", [])
    converted = {
        "handover_id": p.get("handover_id") or p.get("target"),
        "owner_id": p.get("owner_id"),
        "area": p.get("area"),
        "handover_item_ids": items,
    }
    return VerifyHandoverInput.model_validate(converted), review.execution_id


async def verify_handover(request: AgentRequest) -> AgentResult:
    """hr confirm_handover(B)의 교차검증 (read). 거절도 completed + rejected_by_review.

    입력은 계약 `VerifyHandoverInput` 또는 safety 검토 형식(`SafetyReviewInput`) 둘 다 받는다.
    출력은 계약 `VerifyHandoverOutput`이고 safety는 `approved == true`일 때만 승인으로 본다.
    """
    try:
        inp, execution_id = _handover_input(request.input)
    except ValidationError as exc:
        return _invalid(request, exc)
    except ValueError as exc:
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.FAILED,
            outcome="invalid_input",
            output={"message": str(exc)},
        )
    if inp.area is not None and not is_roster_area(inp.area):
        # 비교할 영역을 모르면 승인하지 않는다 (safety는 failed를 무응답으로 보고 보류한다)
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.FAILED,
            outcome="invalid_input",
            output={"message": f"명부에 없는 영역 '{inp.area}' ({', '.join(ROSTER_AREAS)})"},
        )
    check, at = await get_service().verify_handover(
        context_id=request.meta.context_id,
        requester=_requester(request),
        handover_id=inp.handover_id,
        owner_id=inp.owner_id,
        area=inp.area,
        handover_item_ids=inp.handover_item_ids,
    )
    approved = not check.missing
    out = VerifyHandoverOutput(
        approved=approved,
        reason_code="complete" if approved else "missing_items",
        missing=_items(check.missing),
        stale_item_ids=check.stale_item_ids,
        checked_at=at,
    )
    summary = (
        f"{inp.handover_id}: 진행 중 {len(check.current)}건 중 누락 {len(check.missing)}건"
        f", 이미 끝난 항목 {len(check.stale_item_ids)}건"
    )
    if check.unknown_item_ids:
        summary += f" · 확인할 수 없음: {', '.join(check.unknown_item_ids)}"
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=None if approved else "rejected_by_review",
        output=out.model_dump(mode="json"),
        evidence=Evidence(
            queries=[
                f"list_in_progress({_where(inp.owner_id, inp.area)})",
                f"missing = in_progress - handover_item_ids ({len(inp.handover_item_ids)}건)",
            ],
            data_summary=summary,
            ref_ids=[f"handover:{inp.handover_id}"]
            + ([f"execution:{execution_id}"] if execution_id else [])
            + _item_refs(check.current),
        ),
    )


HANDLERS = {
    "create_ticket": create_ticket,
    "find_related_prs": find_related_prs,
    "link_incident": link_incident,
    "verify_rollback_target": verify_rollback_target,
    "list_in_progress": list_in_progress,
    "verify_handover": verify_handover,
}
