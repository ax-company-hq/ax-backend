"""`decide_candidate_card`: 확신이 낮아 후보로 둔 카드를 사람이 승인하거나 폐기한다.

- 승인(`approve`): 카드가 `active`가 되고, 같은 영역의 옛 결정을 바꾸는 카드면 옛 카드를 대체됨으로
  연결한다(후보일 때는 연결하지 않았다).
- 거절(`reject`): 카드가 `retired`가 된다. 판정 근거로 쓰이지 않는다.
사람의 결정이 곧 승인이지만 카드 상태를 바꾸는 일이라 관문(`memory.card_write`)을 거친다.
"""

from __future__ import annotations

from pydantic import ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    DecideCandidateCardInput,
    DecideCandidateCardOutput,
)
from hr_agent.cards.service import link_older_cards
from hr_agent.clients.memory import card_state
from hr_agent.results import (
    completed,
    gate_blocked,
    invalid_input,
    make_evidence,
    refused,
    with_key,
)
from hr_agent.services import Services


async def decide_candidate_card(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = DecideCandidateCardInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    if not request.meta.idempotency_key:
        return invalid_input(request, "상태를 바꾸는 요청에는 idempotency_key가 필요하다")
    card = await svc.memory.get_card(params.card_id)
    if card is None:
        return refused(request, "UNKNOWN_CARD", "없는 카드다")

    target_status = "active" if params.decision == "approve" else "retired"
    state = card_state(card)
    evidence = make_evidence(
        svc.clock, f"후보 카드 {params.decision}: {card.title}", [card.card_id]
    )
    if state == target_status:  # 이미 그 결정이 반영됐다
        svc.audit.record(request.meta.context_id, "card_decision.duplicate", {"card": card.card_id})
        return completed(
            request,
            DecideCandidateCardOutput(card_id=card.card_id, status=target_status),
            outcome="duplicate",
            evidence=evidence,
        )
    if state != "candidate":
        return refused(request, "NOT_CANDIDATE", f"후보 카드가 아니다: {state}")

    target = f"{card.card_id}#{params.decision}"
    decision = await svc.gate.execute(
        "memory.card_write",
        {"card_id": card.card_id, "summary": f"후보 카드 {params.decision}", "target": target},
        with_key(request.meta, f"memory.card_write:{target}"),
        evidence,
    )
    if decision.status != "executed":
        return gate_blocked(request, decision)

    superseded: list[str] = []
    if params.decision == "approve":
        await svc.memory.set_candidate(card.card_id, False)
        approved = await svc.memory.get_card(card.card_id)
        if approved is not None:
            superseded = await link_older_cards(svc, request, approved, evidence)
    else:
        await svc.memory.set_status(card.card_id, "retired")
    svc.audit.record(
        request.meta.context_id,
        f"card.{params.decision}",
        {"card_id": card.card_id, "note": params.note, "superseded": superseded},
    )
    return completed(
        request,
        DecideCandidateCardOutput(card_id=card.card_id, status=target_status),
        outcome="approved" if params.decision == "approve" else "rejected",
        evidence=evidence,
    )
