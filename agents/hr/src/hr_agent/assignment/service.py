"""발령·퇴직·입사: `request_assignment_change` → `confirm_reassignment`(B) →
`confirm_offboarding`(C).

- 요청(A): 확인 목록·후임 후보·권한 목록을 만들고, 인수인계(교대·발령·퇴직) 또는 온보딩(입사)을
  함께 시작한다. 권한 목록은 전달만 하며 회수·부여는 각 부서가 실행한다.
- 재배치 확정(B, 코드 규칙 검증): 변경을 복사본에 적용해 담당 공백·중복이 없을 때만 실제로
  적용하고, 적용 뒤 다시 검증해 문제가 있으면 되돌린다.
- 퇴직 확정(C): 사람이 승인해야 하며, 승인 후 같은 요청을 다시 보내면 적용한다.
- 같은 사람·같은 영역의 변경이 진행 중이면 잠금으로 막는다.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    ChecklistItem,
    ConfirmOffboardingInput,
    ConfirmOffboardingOutput,
    ConfirmReassignmentInput,
    ConfirmReassignmentOutput,
    RequestAssignmentChangeInput,
    RequestAssignmentChangeOutput,
)
from hr_agent.assignment import rules
from hr_agent.clients.safety import VerifyOutcome
from hr_agent.results import (
    completed,
    failed,
    gate_blocked,
    invalid_input,
    make_evidence,
    with_key,
)
from hr_agent.roster import store as roster
from hr_agent.services import Services
from hr_agent.util import dumps, loads, stable_id


# ---- 잠금 -----------------------------------------------------------------------
def _lock_keys(member: str, area: str) -> list[str]:
    return [f"member:{member}", f"area:{area}"]


def _acquire(svc: Services, change_id: str, member: str, area: str) -> str | None:
    """잠금을 잡는다. 다른 변경이 잡고 있으면 그 키를 돌려준다."""
    keys = _lock_keys(member, area)
    for key in keys:
        row = svc.store.one("SELECT owner FROM locks WHERE key = ?", (key,))
        if row and row["owner"] != change_id:
            return key
    for key in keys:
        svc.store.execute(
            "INSERT OR IGNORE INTO locks(key, owner, created_at) VALUES (?, ?, ?)",
            (key, change_id, svc.clock().isoformat()),
        )
    return None


def _release(svc: Services, change_id: str) -> None:
    svc.store.execute("DELETE FROM locks WHERE owner = ?", (change_id,))


# ---- 조회 ----------------------------------------------------------------------
def get_change(svc: Services, change_id: str) -> dict[str, Any] | None:
    row = svc.store.one("SELECT * FROM assignment_changes WHERE change_id = ?", (change_id,))
    if row:
        row["checklist"] = loads(row["checklist"], [])
        row["previous"] = loads(row["previous"], {}) if row["previous"] else {}
    return row


def _set(svc: Services, change_id: str, **fields: Any) -> None:
    fields["updated_at"] = svc.clock().isoformat()
    cols = ", ".join(f"{k} = ?" for k in fields)
    svc.store.execute(
        f"UPDATE assignment_changes SET {cols} WHERE change_id = ?",  # noqa: S608 - 고정 컬럼명
        (*fields.values(), change_id),
    )


def _output(change: dict[str, Any], svc: Services) -> RequestAssignmentChangeOutput:
    kind, area = change["kind"], change["area"]
    return RequestAssignmentChangeOutput(
        change_id=change["change_id"],
        checklist=[ChecklistItem(**c) for c in change["checklist"]],
        successor_candidates=(
            []
            if kind == "join"
            else rules.successor_candidates(svc.store, change["member_id"], area)
        ),
        access_list=rules.access_list(kind, area),
        handover_id=change["handover_id"],
        onboarding_id=change["onboarding_id"],
    )


# ---- 요청 ----------------------------------------------------------------------
def _sub_request(request: AgentRequest, skill: str, payload: dict[str, Any], suffix: str):
    meta = with_key(request.meta, suffix)
    return request.model_copy(update={"skill": skill, "input": payload, "meta": meta})


async def request_assignment_change(svc: Services, request: AgentRequest) -> AgentResult:
    from hr_agent.handover import service as handover
    from hr_agent.onboarding import service as onboarding

    try:
        params = RequestAssignmentChangeInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    kind, member, area = params.change_kind, params.member_id, params.area
    if area not in roster.list_areas(svc.store):
        return failed(request, "UNKNOWN_AREA", f"알 수 없는 영역: {area}")
    in_area = member in {e.member_id for e in roster.entries(svc.store, area)}
    if kind == "join":
        if in_area:
            return failed(request, "ALREADY_IN_AREA", f"{member}은(는) 이미 {area} 영역 담당이다")
    else:
        if not roster.known_member(svc.store, member):
            return failed(request, "UNKNOWN_MEMBER", f"명부에 없는 사람: {member}")
        if not in_area:
            return failed(request, "NOT_IN_AREA", f"{member}은(는) {area} 영역 담당이 아니다")
    if params.successor_id:
        if params.successor_id == member:
            return invalid_input(request, "본인을 후임으로 지정할 수 없다")
        if not roster.known_member(svc.store, params.successor_id):
            return failed(request, "UNKNOWN_MEMBER", f"명부에 없는 사람: {params.successor_id}")

    change_id = stable_id("chg_", request.meta.context_id, kind, member, area)
    existing = get_change(svc, change_id)
    if existing is not None:
        svc.audit.record(request.meta.context_id, "assignment.duplicate", {"id": change_id})
        return completed(
            request,
            _output(existing, svc),
            outcome="duplicate",
            evidence=make_evidence(svc.clock, "이미 요청된 변경", [change_id]),
        )
    blocked = _acquire(svc, change_id, member, area)
    if blocked:
        return failed(request, "LOCKED", f"같은 대상의 다른 변경이 진행 중이다: {blocked}")

    summary = f"{kind} 요청: {member} / {area}" + (
        f" → {params.successor_id}" if params.successor_id else ""
    )
    decision = await svc.gate.execute(
        "request_assignment_change",
        {"change_id": change_id, "summary": summary},
        with_key(request.meta, "request_assignment_change"),
        make_evidence(svc.clock, summary, [change_id]),
    )
    if decision.status != "executed":
        _release(svc, change_id)
        return gate_blocked(request, decision)

    # 인수인계(또는 온보딩)를 함께 시작한다
    handover_id = onboarding_id = None
    if kind == "join":
        sub = await onboarding.start_onboarding(
            svc,
            _sub_request(request, "start_onboarding", {"member_id": member, "area": area}, "onb"),
        )
        onboarding_id = sub.output.get("onboarding_id")
    else:
        sub = await handover.start_handover(
            svc,
            _sub_request(
                request,
                "start_handover",
                {
                    "owner_id": member,
                    "successor_id": params.successor_id,
                    "area": area,
                    "reason": kind,
                },
                "hnd",
            ),
        )
        handover_id = sub.output.get("handover_id")

    now = svc.clock().isoformat()
    previous = {
        "effective_at": params.effective_at.isoformat() if params.effective_at else None,
        "entries": [
            {"area": e.area, "role": e.role, "member_id": e.member_id}
            for e in roster.entries(svc.store)
        ],
    }
    checklist = [
        {"item_id": f"chk_{change_id}_{n}", "title": title, "done": False}
        for n, title in enumerate(rules.CHECKLISTS[kind], start=1)
    ]
    svc.store.execute(
        "INSERT INTO assignment_changes(change_id, context_id, kind, member_id, area,"
        " successor_id, status, handover_id, onboarding_id, checklist, previous, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, ?)",
        (
            change_id,
            request.meta.context_id,
            kind,
            member,
            area,
            params.successor_id,
            handover_id,
            onboarding_id,
            dumps(checklist),
            dumps(previous),
            now,
            now,
        ),
    )
    svc.audit.record(
        request.meta.context_id, "assignment.requested", {"id": change_id, "kind": kind}
    )
    change = get_change(svc, change_id)
    assert change is not None
    return completed(
        request,
        _output(change, svc),
        outcome="requested",
        evidence=make_evidence(
            svc.clock, summary, [change_id, *(handover_id and [handover_id] or [])]
        ),
    )


# ---- 재배치 확정 (B) ---------------------------------------------------------------
def _ops_for(svc: Services, change: dict[str, Any], successor: str) -> list[rules.Op]:
    area_entries = [(e.role, e.member_id) for e in roster.entries(svc.store, change["area"])]
    return rules.plan_ops(
        change["kind"], change["member_id"], change["area"], successor, area_entries
    )


def _apply(svc: Services, ops: list[rules.Op]) -> None:
    for area, role, who, on in ops:
        roster.set_active(svc.store, area, role, who, on)


def _restore(svc: Services, previous: dict[str, Any]) -> None:
    """변경 전 명부로 되돌린다(적용 뒤 검증이 실패했을 때)."""
    keep = {(e["area"], e["role"], e["member_id"]) for e in previous.get("entries", [])}
    svc.store.execute("UPDATE roster_entries SET active = 0")
    for area, role, who in keep:
        roster.set_active(svc.store, area, role, who, True)


async def confirm_reassignment(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = ConfirmReassignmentInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    change = get_change(svc, params.change_id)
    if change is None:
        return failed(request, "UNKNOWN_CHANGE", "없는 변경 요청이다")
    kind, member = change["kind"], change["member_id"]
    successor = params.successor_id
    if change["status"] == "confirmed":
        return _reassign_result(
            request, svc, change, "confirmed", rules.Coverage([], []), "duplicate"
        )
    if change["status"] != "requested":
        return failed(request, "BAD_STATE", f"확정할 수 없는 상태다: {change['status']}")
    if kind == "join":
        if successor != member:
            return invalid_input(request, "입사는 successor_id에 입사자 본인을 넣는다")
    elif not roster.known_member(svc.store, successor) or successor == member:
        return invalid_input(request, "후임이 올바르지 않다")

    ops = _ops_for(svc, change, successor)
    coverage, _ = rules.simulate(svc.store, ops)
    gate_params = {
        "change_id": change["change_id"],
        "ops": [list(op) for op in ops],
        "summary": f"{kind} 재배치 {member} → {successor} ({change['area']})",
    }
    evidence = make_evidence(svc.clock, gate_params["summary"], [change["change_id"]])
    decision = await svc.gate.execute(
        "confirm_reassignment",
        gate_params,
        with_key(request.meta, "confirm_reassignment"),
        evidence,
    )
    if decision.status != "executed":
        svc.audit.record(
            request.meta.context_id,
            "reassignment.rejected",
            {"gap": coverage.gap_areas, "dup": coverage.duplicate_areas},
        )
        return _reassign_result(request, svc, change, "rejected", coverage, "rejected", evidence)

    _apply(svc, ops)
    touched = {a for a, *_ in ops}  # 적용 뒤 다시 검증
    post = rules.check_coverage(rules.entries_map(svc.store), sorted(touched))
    if not post.ok:  # 검증을 통과했는데도 문제가 생기면 되돌린다
        _restore(svc, change["previous"])
        svc.audit.record(
            request.meta.context_id, "reassignment.undone", {"id": change["change_id"]}
        )
        return _reassign_result(request, svc, change, "rejected", post, "rejected", evidence)
    if kind == "join":
        roster.add_member(svc.store, member)
    if kind != "offboarding":
        _release(svc, change["change_id"])
    _set(svc, change["change_id"], status="confirmed", successor_id=successor)
    svc.audit.record(
        request.meta.context_id,
        "reassignment.confirmed",
        {"id": change["change_id"], "ops": len(ops)},
    )
    change = get_change(svc, change["change_id"])
    assert change is not None
    return _reassign_result(request, svc, change, "confirmed", post, "confirmed", evidence)


def _reassign_result(
    request: AgentRequest,
    svc: Services,
    change: dict[str, Any],
    status: str,
    coverage: rules.Coverage,
    outcome: str,
    evidence: Any = None,
) -> AgentResult:
    output = ConfirmReassignmentOutput(
        change_id=change["change_id"],
        status=status,  # type: ignore[arg-type]
        gap_areas=coverage.gap_areas,
        duplicate_areas=coverage.duplicate_areas,
    )
    return completed(
        request,
        output,
        outcome=outcome,
        evidence=evidence or make_evidence(svc.clock, f"재배치 {status}", [change["change_id"]]),
    )


def register_verifiers(svc: Services) -> None:
    """MockGate에 재배치 검증자(코드 규칙)를 등록한다: 변경 뒤 담당 공백·중복이 없어야 통과."""
    register = getattr(svc.gate, "register_verifier", None)
    if register is None:
        return

    async def verify(params: dict[str, Any], _context: Any) -> VerifyOutcome:
        ops = [(a, r, w, bool(on)) for a, r, w, on in params["ops"]]
        coverage, _ = rules.simulate(svc.store, ops)
        return VerifyOutcome(
            coverage.ok, {"gap": coverage.gap_areas, "duplicate": coverage.duplicate_areas}
        )

    register("confirm_reassignment", verify)


# ---- 퇴직 확정 (C) ----------------------------------------------------------------
async def confirm_offboarding(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = ConfirmOffboardingInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    change = get_change(svc, params.change_id)
    if change is None:
        return failed(request, "UNKNOWN_CHANGE", "없는 변경 요청이다")
    if change["kind"] != "offboarding":
        return failed(request, "NOT_OFFBOARDING", "퇴직 요청이 아니다")
    if change["status"] == "offboarded":
        return _off_result(request, svc, change, "confirmed", None, "duplicate")
    if change["status"] != "confirmed":
        return failed(request, "REASSIGN_FIRST", "재배치 확정이 먼저 필요하다")

    member = change["member_id"]
    summary = f"퇴직 확정 요청: {member} ({change['area']})"
    evidence = make_evidence(svc.clock, summary, [change["change_id"]])
    decision = await svc.gate.execute(
        "confirm_offboarding",
        {"change_id": change["change_id"], "summary": summary},
        with_key(request.meta, "confirm_offboarding"),
        evidence,
    )
    if decision.status == "held":
        svc.audit.record(request.meta.context_id, "offboarding.held", {"id": change["change_id"]})
        return _off_result(request, svc, change, "held", decision.review_id, "held", evidence)
    if decision.status != "executed":
        return gate_blocked(request, decision)

    # 승인 뒤 적용: 남은 모든 담당에서 빠졌을 때 공백이 생기면 하지 않는다(fail-closed)
    coverage, _ = rules.simulate(svc.store, [], extra_removals=[member])
    if not coverage.ok:
        svc.audit.record(
            request.meta.context_id, "offboarding.blocked", {"gap": coverage.gap_areas}
        )
        return failed(
            request,
            "COVERAGE_GAP",
            "퇴직하면 담당이 비는 영역이 있어 적용하지 않았다: " + ", ".join(coverage.gap_areas),
        )
    for entry in roster.entries(svc.store, member_id=member):
        roster.set_active(svc.store, entry.area, entry.role, member, False)
    roster.deactivate_member(svc.store, member)
    _release(svc, change["change_id"])
    _set(svc, change["change_id"], status="offboarded")
    svc.audit.record(request.meta.context_id, "offboarding.confirmed", {"id": change["change_id"]})
    change = get_change(svc, change["change_id"])
    assert change is not None
    return _off_result(request, svc, change, "confirmed", None, "confirmed", evidence)


def _off_result(
    request: AgentRequest,
    svc: Services,
    change: dict[str, Any],
    status: str,
    hold_id: str | None,
    outcome: str,
    evidence: Any = None,
) -> AgentResult:
    output = ConfirmOffboardingOutput(
        change_id=change["change_id"],
        status=status,  # type: ignore[arg-type]
        hold_id=hold_id,
    )
    return completed(
        request,
        output,
        outcome=outcome,
        evidence=evidence or make_evidence(svc.clock, f"퇴직 {status}", [change["change_id"]]),
    )
