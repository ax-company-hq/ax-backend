"""`record_case`: 부서가 보낸 사건·결정 기록을 결정 카드로 만들어 회사 기억에 저장한다.

처리 순서(인사 구성안 HR2 3장):
1. 입력 검증(`RecordCase`) → 같은 기록이면 중복 처리(멱등성)
2. 결정 탐지: 규칙(구조화 필드) → 규칙 추출(낮은 확신도) → 결정 없음(기록만)
3. 카드 조립(근거 없는 값은 "확인할 수 없음", 확신도 규칙)
4. 저장: `execute("memory.card_write")`가 실행됨을 돌려준 뒤에만 memory에 쓴다
5. 같은 영역의 옛 결정을 대체하면 옛 카드를 `superseded`로 연결
6. 감사 로그와 결과(카드 ID·확신도·근거)

상태를 바꾸는 호출은 모두 관문을 거치고, 관문이 막으면(거부·보류) 카드를 쓰지 않는다.
"""

from __future__ import annotations

from pydantic import ValidationError

from ax_contracts.schemas import AgentRequest, AgentResult, RecordCase, RecordCaseOutput
from hr_agent.cards.builder import (
    build_card,
    card_id_of,
    evidence_refs,
    target_key,
)
from hr_agent.cards.decide import detect_decision
from hr_agent.cards.link import find_superseded
from hr_agent.results import (
    completed,
    gate_blocked,
    invalid_input,
    make_evidence,
    with_key,
)
from hr_agent.services import Services
from hr_agent.util import dumps


def idempotency_key_of(record: RecordCase) -> str:
    """`context_id + record_case + 대상`: 같은 이벤트가 두 번 와도 카드는 한 번만 만든다."""
    return f"record_case:{record.context_id}:{record.kind.value}:{target_key(record)}"


async def record_case(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        record = RecordCase.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")

    refs = evidence_refs(record)
    ev_summary = (
        f"기록 근거 {len(refs)}건: " + ", ".join(refs) if refs else "근거 없음: 확인할 수 없음"
    )
    evidence = make_evidence(svc.clock, ev_summary, refs)
    keep = {
        "decision_reason": record.decision_reason,
        "alternatives": record.alternatives_considered,
    }
    key = idempotency_key_of(record)

    prior = svc.idem.get(key)
    if prior is not None:
        svc.audit.record(record.context_id, "record_case.duplicate", {"key": key})
        output = RecordCaseOutput(
            outcome="duplicate", card_id=prior.get("card_id"), confidence=prior.get("confidence")
        )
        return completed(
            request,
            output,
            outcome="duplicate",
            evidence=evidence,
            decision_reason=keep["decision_reason"],
            alternatives=keep["alternatives"],
        )

    info = detect_decision(record)
    if info is None:  # 결정이 없는 기록: 카드를 만들지 않고 기록만 남긴다
        decision = await svc.gate.execute(
            "record_case",
            {"summary": record.summary, "kind": record.kind.value, "target": key},
            with_key(request.meta, f"record_case:{key}"),
            evidence,
        )
        if decision.status != "executed":
            return gate_blocked(request, decision)
        svc.store.execute(
            "INSERT OR IGNORE INTO records(record_id, tenant_id, context_id, kind, summary,"
            " source_refs, author, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card_id_of(record).replace("card_", "rec_"),
                record.tenant_id,
                record.context_id,
                record.kind.value,
                record.summary,
                dumps(record.source_refs),
                request.meta.requester or record.author,
                svc.clock().isoformat(),
            ),
        )
        svc.idem.put(key, {"outcome": "record_only"})
        svc.audit.record(record.context_id, "record_only", {"kind": record.kind.value})
        return completed(
            request,
            RecordCaseOutput(outcome="record_only"),
            outcome="record_only",
            evidence=evidence,
            decision_reason=keep["decision_reason"],
            alternatives=keep["alternatives"],
        )

    card = build_card(
        record, info, svc.clock(), svc.settings.card_auto_confirm, author=request.meta.requester
    )
    fallback = False
    if svc.narrator.enabled:  # LLM 문장은 교훈 한 줄만. 실패하면 붙이지 않고 fallback으로 표시한다
        lesson = await svc.narrator.sentence(
            "교훈과 재발 방지 한 줄",
            {"decision": info.decision, "reason": info.reason, "alternatives": info.alternatives},
            "",
            untrusted=dict(request.untrusted),
        )
        fallback = lesson.fallback
        if lesson.text and not lesson.fallback:
            card = card.model_copy(update={"body": f"{card.body}\n교훈: {lesson.text}"})

    decision = await svc.gate.execute(
        "memory.card_write",
        {
            "card_id": card.card_id,
            "summary": card.title,
            "confidence": card.confidence,
            "target": card.card_id,
        },
        with_key(request.meta, f"memory.card_write:{card.card_id}"),
        evidence,
    )
    if decision.status != "executed":
        svc.audit.record(record.context_id, "card_write_blocked", {"status": decision.status})
        return gate_blocked(request, decision)
    await svc.memory.write_card(card)

    superseded: list[str] = []
    for old_id in await find_superseded(card, svc.memory):
        link_decision = await svc.gate.execute(
            "memory.card_link",
            {
                "card_id": old_id,
                "summary": f"{old_id} 대체",
                "op": "supersede",
                "target": f"{old_id}>{card.card_id}",
            },
            with_key(request.meta, f"memory.card_link:{old_id}>{card.card_id}"),
            evidence,
        )
        if link_decision.status == "executed":
            await svc.memory.set_status(old_id, "superseded", card.card_id)
            superseded.append(old_id)

    candidate = bool(card.extra["candidate"])
    outcome = "candidate" if candidate else "card_created"
    svc.idem.put(key, {"outcome": outcome, "card_id": card.card_id, "confidence": card.confidence})
    svc.audit.record(
        record.context_id,
        f"record_case.{outcome}",
        {"card_id": card.card_id, "confidence": card.confidence, "superseded": superseded},
    )
    return completed(
        request,
        RecordCaseOutput(outcome=outcome, card_id=card.card_id, confidence=card.confidence),
        outcome=outcome,
        evidence=make_evidence(svc.clock, ev_summary, [*refs, card.card_id]),
        decision_reason=keep["decision_reason"],
        alternatives=keep["alternatives"],
        fallback=fallback,
    )
