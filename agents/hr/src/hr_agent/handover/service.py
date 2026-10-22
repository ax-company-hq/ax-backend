"""인수인계: `start_handover`(초안·빈자리 질문) → `answer_handover_question` → `confirm_handover`.

- 필수 항목 5개(`fields.FIELDS`) 중 채운 비율이 완성도다. 코드가 계산하고 LLM은 질문 문장만
  다듬는다.
- 진행 중인 일은 planning에서, 결정은 회사 기억의 해당 영역 카드에서, 연락처는 명부에서 자동으로
  채운다. 나머지는 인계자에게 묻는다(입력 대기 `human_input`, 대기 상태는 hr이 저장).
- 확정: 완성도가 기준 이상이면 B(planning 검증), 미만이면 C(보류 큐)로 관문에 올린다.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    AnswerHandoverQuestionInput,
    AnswerHandoverQuestionOutput,
    ConfirmHandoverInput,
    ConfirmHandoverOutput,
    HandoverItem,
    ListInProgressInput,
    StartHandoverInput,
    StartHandoverOutput,
    VerifyHandoverInput,
)
from hr_agent.clients.memory import card_state
from hr_agent.clients.safety import VerifyOutcome
from hr_agent.handover import store as hstore
from hr_agent.handover.fields import FIELD_LABEL, FIELDS, question_id
from hr_agent.narrative import Sentence
from hr_agent.results import (
    completed,
    gate_blocked,
    input_required,
    invalid_input,
    make_evidence,
    refused,
    with_key,
)
from hr_agent.roster import store as roster
from hr_agent.services import Services
from hr_agent.util import stable_id


def handover_id_of(request: AgentRequest, owner: str, area: str, successor: str | None) -> str:
    return stable_id("hnd_", request.meta.context_id, owner, area, successor or "")


async def _area_cards(svc: Services, area: str) -> list[HandoverItem]:
    out = []
    for card in await svc.memory.list_cards():
        extra = card.extra or {}
        if extra.get("area") == area and card_state(card) == "active" and extra.get("decision"):
            out.append(
                HandoverItem(
                    item_id=card.card_id, kind="decision", title=card.title, source_ref=card.card_id
                )
            )
    return out


async def _question_text(svc: Services, field: str, area: str) -> Sentence:
    label = FIELD_LABEL[field]
    template = f"{area} 영역의 '{label}'이(가) 비어 있어요. 후임이 알아야 할 내용을 알려 주세요."
    sentence = await svc.narrator.sentence(
        "인계자에게 빈 항목을 채워 달라고 묻는 질문", {"area": area, "field": label}, template
    )
    return sentence


def _output(svc: Services, handover_id: str) -> StartHandoverOutput:
    row = hstore.get(svc.store, handover_id)
    assert row is not None
    questions = hstore.open_questions(svc.store, handover_id)
    return StartHandoverOutput(
        handover_id=handover_id,
        status=row["status"],
        items=hstore.items(svc.store, handover_id),
        completeness=row["completeness"],
        gap_count=len(questions),
        questions=questions,
    )


def _result_for(
    svc: Services,
    request: AgentRequest,
    handover_id: str,
    summary: str,
    refs: list[str],
    fallback: bool = False,
) -> AgentResult:
    output = _output(svc, handover_id)
    evidence = make_evidence(svc.clock, summary, refs)
    if output.gap_count:
        return input_required(request, output, evidence=evidence, fallback=fallback)
    return completed(request, output, outcome="ready", evidence=evidence, fallback=fallback)


async def start_handover(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = StartHandoverInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    owner = params.owner_id
    owner_areas = roster.areas_of(svc.store, owner)
    if not owner_areas:
        return refused(request, "UNKNOWN_MEMBER", f"명부에 없는 인계자: {owner}")
    area = params.area or (owner_areas[0] if len(owner_areas) == 1 else None)
    if area is None:
        return invalid_input(request, "인계자의 담당 영역이 여러 개라 area가 필요하다")
    if area not in owner_areas:
        return refused(request, "NOT_IN_AREA", f"{owner}은(는) {area} 영역 담당이 아니다")
    if params.successor_id == owner:
        return invalid_input(request, "인계자와 후임이 같다")
    if params.successor_id and not roster.active_member(svc.store, params.successor_id):
        return refused(request, "UNKNOWN_MEMBER", f"재직 중인 사람이 아니다: {params.successor_id}")

    handover_id = handover_id_of(request, owner, area, params.successor_id)
    if (
        hstore.get(svc.store, handover_id) is not None
    ):  # 같은 요청 반복: 현재 상태를 그대로 돌려준다
        svc.audit.record(request.meta.context_id, "start_handover.duplicate", {"id": handover_id})
        again = hstore.reask_stale(
            svc.store, handover_id, svc.clock(), svc.settings.handover_question_ttl_minutes
        )
        if again:  # 답이 없는 질문은 기한이 지나면 다시 묻는다
            svc.audit.record(
                request.meta.context_id, "handover.question_reasked", {"questions": again}
            )
        return _result_for(svc, request, handover_id, "이미 만들어진 인수인계 초안", [handover_id])

    # 사실 수집: planning 진행 중 항목 + 영역 결정 카드 + 명부 연락처
    in_progress = await svc.planning.list_in_progress(
        ListInProgressInput(owner_id=owner, area=area)
    )
    items = [
        HandoverItem(item_id=i.item_id, kind=i.kind, title=i.title, source_ref=i.item_id)
        for i in in_progress.items
    ]
    decision_items = await _area_cards(svc, area)
    contacts = [e.member_id for e in roster.entries(svc.store, area) if e.member_id != owner]
    auto = {
        "in_progress": bool(items),
        "decisions": bool(decision_items),
        "contacts": bool(contacts),
    }
    refs = [i.item_id for i in items + decision_items]
    summary = (
        f"{owner} → {params.successor_id or '후임 미정'} / {area}: "
        f"진행 중 {len(items)}건, 결정 카드 {len(decision_items)}장, 연락처 {len(contacts)}명"
    )

    decision = await svc.gate.execute(
        "start_handover",
        {"handover_id": handover_id, "summary": summary, "target": handover_id},
        with_key(request.meta, f"start_handover:{handover_id}"),
        make_evidence(svc.clock, summary, refs),
    )
    if decision.status != "executed":
        return gate_blocked(request, decision)

    now = svc.clock().isoformat()
    # 질문 문장은 DB 트랜잭션 밖에서 만든다(LLM 호출이 느려도 잠금을 오래 잡지 않는다)
    texts: dict[str, Sentence] = {
        field: await _question_text(svc, field, area)
        for field, _label in FIELDS
        if not auto.get(field, False)
    }
    fallback = any(t.fallback for t in texts.values())
    if hstore.get(svc.store, handover_id) is not None:  # 기다리는 사이 같은 요청이 먼저 끝났다
        return _result_for(svc, request, handover_id, "이미 만들어진 인수인계 초안", [handover_id])
    with svc.store.transaction():
        svc.store.execute(
            "INSERT INTO handovers(handover_id, context_id, owner_id, successor_id, area, reason,"
            " status, completeness, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                handover_id,
                request.meta.context_id,
                owner,
                params.successor_id,
                area,
                params.reason,
                "draft",
                0,
                now,
                now,
            ),
        )
        for item in [*items, *decision_items]:
            svc.store.execute(
                "INSERT OR IGNORE INTO handover_items(handover_id, item_id, kind, title,"
                " source_ref) VALUES (?, ?, ?, ?, ?)",
                (handover_id, item.item_id, item.kind, item.title, item.source_ref),
            )
        for field, _label in FIELDS:
            filled = auto.get(field, False)
            svc.store.execute(
                "INSERT INTO handover_fields(handover_id, field, filled, source, answer)"
                " VALUES (?, ?, ?, ?, ?)",
                (handover_id, field, 1 if filled else 0, "auto" if filled else "none", None),
            )
            if not filled:
                svc.store.execute(
                    "INSERT INTO handover_questions(question_id, handover_id, context_id,"
                    " assignee_id, question, gap_field, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                    (
                        question_id(handover_id, field),
                        handover_id,
                        request.meta.context_id,
                        owner,
                        texts[field].text,
                        field,
                        now,
                    ),
                )
    pct, status = hstore.refresh(svc.store, handover_id, now)
    svc.audit.record(
        request.meta.context_id,
        "start_handover",
        {"id": handover_id, "completeness": pct, "status": status},
    )
    return _result_for(svc, request, handover_id, summary, refs, fallback)


async def answer_handover_question(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = AnswerHandoverQuestionInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    answer = params.answer.strip()
    if not answer:
        return invalid_input(request, "답변이 비어 있다")
    q = hstore.question(svc.store, params.question_id)
    if q is None:
        return refused(request, "UNKNOWN_QUESTION", "없는 질문이다")
    handover_id = q["handover_id"]

    if q["status"] == "answered":  # 같은 답이 다시 오면 한 번만 반영된 상태를 돌려준다
        if q["answer"] != answer:
            return refused(request, "CONFLICT", "이미 다른 답변이 반영된 질문이다")
        svc.audit.record(request.meta.context_id, "answer.duplicate", {"q": q["question_id"]})
        return _answer_result(svc, request, handover_id, "이미 반영된 답변", duplicate=True)
    row = hstore.get(svc.store, handover_id)
    if row and row["status"] in ("confirmed",):
        return refused(request, "HANDOVER_CLOSED", "이미 확정된 인수인계다")

    decision = await svc.gate.execute(
        "answer_handover_question",
        {
            "question_id": q["question_id"],
            "summary": f"{q['gap_field']} 답변",
            "target": q["question_id"],
        },
        with_key(request.meta, f"answer:{q['question_id']}"),
        make_evidence(svc.clock, f"인계자 답변: {q['gap_field']}", [q["question_id"]]),
    )
    if decision.status != "executed":
        return gate_blocked(request, decision)

    now = svc.clock().isoformat()
    with svc.store.transaction():
        svc.store.execute(
            "UPDATE handover_questions SET status = 'answered', answer = ?, answered_at = ?"
            " WHERE question_id = ?",
            (answer, now, q["question_id"]),
        )
        svc.store.execute(
            "UPDATE handover_fields SET filled = 1, source = 'answer', answer = ?"
            " WHERE handover_id = ? AND field = ?",
            (answer, handover_id, q["gap_field"]),
        )
    pct, status = hstore.refresh(svc.store, handover_id, now)
    svc.audit.record(
        request.meta.context_id,
        "answer_handover_question",
        {"q": q["question_id"], "completeness": pct, "status": status},
    )
    return _answer_result(svc, request, handover_id, f"{q['gap_field']} 답변 반영")


def _answer_result(
    svc: Services, request: AgentRequest, handover_id: str, summary: str, duplicate: bool = False
) -> AgentResult:
    row = hstore.get(svc.store, handover_id)
    assert row is not None
    output = AnswerHandoverQuestionOutput(
        handover_id=handover_id,
        status=row["status"],
        completeness=row["completeness"],
        remaining_questions=hstore.open_questions(svc.store, handover_id),
    )
    return completed(
        request,
        output,
        outcome="duplicate" if duplicate else "answered",
        evidence=make_evidence(svc.clock, summary, [handover_id]),
    )


async def confirm_handover(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = ConfirmHandoverInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    row = hstore.get(svc.store, params.handover_id)
    if row is None:
        return refused(request, "UNKNOWN_HANDOVER", "없는 인수인계다")
    pct = row["completeness"]
    if row["status"] == "confirmed":
        return _confirm_result(request, svc, row, "confirmed", pct, [], None, "already_confirmed")

    item_ids = [i.item_id for i in hstore.items(svc.store, row["handover_id"])]
    below = pct < svc.settings.handover_min_completeness
    action = "confirm_handover_incomplete" if below else "confirm_handover"
    params_for_gate: dict[str, Any] = {
        "handover_id": row["handover_id"],
        "owner_id": row["owner_id"],
        "area": row["area"],
        "item_ids": item_ids,
        "completeness": pct,
        "target": row["handover_id"],
        "summary": (
            f"인수인계 확정 요청 {row['owner_id']}→{row['successor_id'] or '미정'} "
            f"({row['area']}), 완성도 {pct}%"
        ),
    }
    evidence = make_evidence(svc.clock, params_for_gate["summary"], [row["handover_id"], *item_ids])
    decision = await svc.gate.execute(
        action,
        params_for_gate,
        with_key(request.meta, f"{action}:{row['handover_id']}:{pct}"),
        evidence,
    )
    now = svc.clock().isoformat()
    if decision.status == "held":
        hstore.set_status(svc.store, row["handover_id"], "held", now)
        svc.audit.record(
            request.meta.context_id, "confirm_handover.held", {"id": row["handover_id"], "pct": pct}
        )
        return _confirm_result(
            request, svc, row, "held", pct, [], decision.review_id, "held", evidence=evidence
        )
    if decision.status == "executed":
        hstore.set_status(svc.store, row["handover_id"], "confirmed", now)
        svc.audit.record(request.meta.context_id, "confirm_handover", {"id": row["handover_id"]})
        return _confirm_result(
            request,
            svc,
            {**row, "status": "confirmed"},
            "confirmed",
            pct,
            [],
            None,
            "confirmed",
            evidence=evidence,
        )
    hstore.set_status(svc.store, row["handover_id"], "input_required", now)
    missing: list[str] = []
    if decision.grade == "B":  # 검증자(planning)가 거절했다: 무엇이 빠졌는지 읽기 전용으로 확인한다
        verdict = await svc.planning.verify_handover(
            VerifyHandoverInput(
                handover_id=row["handover_id"],
                owner_id=row["owner_id"],
                area=row["area"],
                handover_item_ids=item_ids,
            )
        )
        missing = [i.item_id for i in verdict.missing]
    svc.audit.record(
        request.meta.context_id,
        "confirm_handover.rejected",
        {"grade": decision.grade, "reason": decision.hold_reason, "missing": missing},
    )
    return _confirm_result(request, svc, row, "rejected", pct, missing, None, "rejected", evidence)


def _confirm_result(
    request: AgentRequest,
    svc: Services,
    row: dict[str, Any],
    status: str,
    pct: int,
    missing: list[str],
    hold_id: str | None,
    outcome: str,
    evidence: Any = None,
) -> AgentResult:
    output = ConfirmHandoverOutput(
        handover_id=row["handover_id"],
        status=status,  # type: ignore[arg-type]
        completeness=pct,
        missing_item_ids=missing,
        hold_id=hold_id,
    )
    evidence = evidence or make_evidence(svc.clock, f"인수인계 {status}", [row["handover_id"]])
    if status == "held":  # C 보류는 사람 승인을 기다리는 input-required다
        return input_required(request, output, evidence=evidence)
    return completed(request, output, outcome=outcome, evidence=evidence)


def register_verifiers(svc: Services) -> None:
    """MockGate에 B 등급 검증자를 등록한다: 진행 중 항목이 인계 목록에 빠짐없이 있는지 확인."""
    register = getattr(svc.gate, "register_verifier", None)
    if register is None:
        return

    async def verify(params: dict[str, Any], _context: Any) -> VerifyOutcome:
        verdict = await svc.planning.verify_handover(
            VerifyHandoverInput(
                handover_id=params["handover_id"],
                owner_id=params["owner_id"],
                area=params["area"],
                handover_item_ids=list(params.get("item_ids", [])),
            )
        )
        return VerifyOutcome(verdict.approved, {"missing": [i.item_id for i in verdict.missing]})

    register("confirm_handover", verify)
