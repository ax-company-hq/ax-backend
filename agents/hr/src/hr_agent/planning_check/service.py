"""`check_plan`: 새 계획 초안을 회사 기억의 결정 카드와 비교한다(조회 전용).

판정은 모두 코드 규칙이다(LLM은 설명 문장만 다듬고, 실패하면 고정 문장을 쓴다).
- `REJECTED_BEFORE`(warn): 같은 영역의 *기각된* 유효 카드와 비슷하다. 경과 일수는 코드가 계산한다.
- `CONFLICT`(warn): 채택된 카드가 "검토했지만 채택하지 않은 대안"과 초안이 같다.
- `MATCH`(info): 채택된 카드와 같은 방향이다.
- 관련 카드가 없으면 판정하지 않고 `no_related_cards`로 "기록에 없음"을 알린다.
후보(확신도 낮은) 카드는 참고 목록에만 넣고 판정 근거로 쓰지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    Card,
    CheckPlanComputed,
    CheckPlanInput,
    CheckPlanOutput,
    PlanFinding,
    SimilarCard,
)
from hr_agent.clients.memory import CardHit, card_state
from hr_agent.results import completed, invalid_input, make_evidence, refused
from hr_agent.roster import store as roster
from hr_agent.services import Services
from hr_agent.util import overlap, similarity

MATCH_SCORE = 0.4  # 이 이상이면 같은 방향의 결정으로 본다
ALT_SCORE = 0.3  # 초안이 "채택하지 않은 대안"과 이 이상 비슷하면 충돌


def _decided_at(card: Card) -> datetime:
    raw = card.extra.get("decided_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).astimezone(UTC)
        except ValueError:
            pass
    return card.created_at.astimezone(UTC)


def _score(card: Card, draft_text: str) -> float:
    """초안과 카드 핵심 문장(제목·결정·채택하지 않은 대안)의 유사도. 길이 차이에 강하다."""
    alternatives = " ".join(str(a) for a in card.extra.get("alternatives_rejected") or [])
    key = f"{card.title} {card.extra.get('decision', '')} {alternatives}"
    return round(max(similarity(draft_text, key), overlap(key, draft_text)), 4)


def _is_rejected(card: Card) -> bool:
    return card.extra.get("outcome") == "rejected"


def _conflicting_alternative(card: Card, draft_text: str) -> str | None:
    for alt in card.extra.get("alternatives_rejected") or []:
        alt = str(alt).strip()
        if alt and (alt in draft_text or similarity(alt, draft_text) >= ALT_SCORE):
            return alt
    return None


async def check_plan(svc: Services, request: AgentRequest) -> AgentResult:
    try:
        params = CheckPlanInput.model_validate(request.input)
    except ValidationError as exc:
        return invalid_input(request, exc)
    draft = params.draft
    if draft.area not in roster.list_areas(svc.store):
        return refused(request, "UNKNOWN_AREA", f"알 수 없는 영역: {draft.area}")
    if not draft.title.strip():
        return invalid_input(request, "계획 제목이 비어 있다")

    now = svc.clock()
    draft_text = f"{draft.title} {draft.summary}"
    found = await svc.memory.search_cards(
        draft_text, area=draft.area, states=["active", "candidate"], limit=500
    )
    rescored = [CardHit(h.card, _score(h.card, draft_text)) for h in found]
    hits: list[CardHit] = sorted(
        (h for h in rescored if h.score >= svc.settings.similarity_threshold),
        key=lambda h: (-h.score, h.card.card_id),
    )[:5]
    similar = [
        SimilarCard(card_id=h.card.card_id, summary=h.card.title, status=card_state(h.card))
        for h in hits
    ]
    firm = [h for h in hits if card_state(h.card) == "active"]  # 후보는 판정 근거로 쓰지 않는다

    findings: list[PlanFinding] = []
    computed = CheckPlanComputed()
    for hit in firm:
        card = hit.card
        if _is_rejected(card):
            days = max(0, (now.astimezone(UTC).date() - _decided_at(card).date()).days)
            reason = card.extra.get("reason") or "기록에 없음"
            # 경고 문장은 고정 문장이다: 경과 일수 같은 숫자를 LLM 문장에 맡기지 않는다
            message = f"이미 기각된 안과 비슷하다: {card.title} ({days}일 전 기각, 이유: {reason})"
            findings.append(
                PlanFinding(
                    code="REJECTED_BEFORE",
                    level="warn",
                    message=message,
                    card_ids=[card.card_id],
                )
            )
            if computed.days_since_rejected is None or days < computed.days_since_rejected:
                computed = CheckPlanComputed(
                    days_since_rejected=days,
                    basis=f"기각 카드 {card.card_id}의 결정 시각 {_decided_at(card).isoformat()}",
                )
            continue
        alt = _conflicting_alternative(card, draft_text)
        if alt:
            findings.append(
                PlanFinding(
                    code="CONFLICT",
                    level="warn",
                    message=f"채택하지 않기로 한 대안과 같다: '{alt}' ({card.title})",
                    card_ids=[card.card_id],
                )
            )
        elif hit.score >= MATCH_SCORE:
            findings.append(
                PlanFinding(
                    code="MATCH",
                    level="info",
                    message=f"이미 확정된 결정과 같은 방향이다: {card.title}",
                    card_ids=[card.card_id],
                )
            )

    verdict = "warn" if any(f.level == "warn" for f in findings) else "ok"
    output = CheckPlanOutput(
        verdict=verdict,
        findings=findings,
        similar_cards=similar,
        computed=computed,
        no_related_cards=not hits,
    )
    refs = [c.card_id for c in similar]
    summary = (
        f"{draft.area} 영역 관련 카드 {len(hits)}장 비교, 판정 {verdict}"
        if hits
        else f"{draft.area} 영역에 관련 카드 없음: 기록에 없음 (조회 {now.isoformat()})"
    )
    svc.audit.record(
        request.meta.context_id,
        "check_plan",
        {"area": draft.area, "verdict": verdict, "findings": [f.code for f in findings]},
    )
    return completed(
        request,
        output,
        outcome=verdict,
        evidence=make_evidence(svc.clock, summary, refs),
    )
