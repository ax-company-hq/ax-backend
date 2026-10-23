"""카드 조립: `RecordCase` + 결정 정보 → 계약 `Card`. 순수 함수라서 같은 입력이면 같은 카드다.

- 카드 ID는 `context_id`·종류·대상으로 정해서 같은 기록이 두 번 와도 같은 카드가 된다.
- 근거 없는 값은 지어내지 않고 "확인할 수 없음"으로 적는다.
- 확신도 규칙(기획안 세부 기준 미정, 제안값): 이유·대안·근거가 모두 있으면 `high`, 둘이 있으면
  `mid`, 그 밖에는 `low`. 규칙으로 뽑은 이유(`extracted`)는 항상 `low`, 문장에서 읽은 기각은
  최대 `mid`다.
  자동 확정은 설정(`card_auto_confirm`, 기본 `high`)과 같은 확신도일 때만이고, 그 밖은 후보다.
- 계약의 `RecordCase`에는 `area`가 없어서 `source_refs`의 `area:<영역>` 항목에서 읽는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from ax_contracts.schemas import Card, RecordCase
from hr_agent.cards.decide import DecisionInfo
from hr_agent.cards.mapping import CARD_KIND, LABEL, outcome_of
from hr_agent.util import stable_id

UNKNOWN = "확인할 수 없음"
Confidence = Literal["low", "mid", "high"]
AREA_PREFIX = "area:"


def area_of(record: RecordCase) -> str | None:
    """영역: 계약의 `area` 필드가 먼저, 없으면 근거 참조의 `area:<영역>`(옛 호출 방식)."""
    if record.area and record.area.strip():
        return record.area.strip()
    for ref in record.source_refs:
        if ref.startswith(AREA_PREFIX):
            return ref[len(AREA_PREFIX) :].strip() or None
    return None


def evidence_refs(record: RecordCase) -> list[str]:
    """근거 참조: `area:` 표시는 근거가 아니라 영역 지정이라서 뺀다."""
    return [r for r in record.source_refs if not r.startswith(AREA_PREFIX)]


def target_key(record: RecordCase) -> str:
    """같은 기록을 알아보는 대상 키: 첫 근거 참조, 없으면 요약."""
    refs = evidence_refs(record)
    return refs[0] if refs else record.summary.strip()


def card_id_of(record: RecordCase) -> str:
    return stable_id("card_", record.context_id, record.kind.value, target_key(record))


def confidence_of(record: RecordCase, info: DecisionInfo) -> Confidence:
    if info.extracted or not info.reason:
        return "low"
    present = sum([bool(info.reason), bool(info.alternatives), bool(evidence_refs(record))])
    if present == 3:
        # 문장에서 읽은 기각 표현은 최대 mid(후보): 잘못 읽은 기각이 경고로 번지지 않게 한다
        return "mid" if outcome_inferred(record, info) else "high"
    return "mid" if present == 2 else "low"


def outcome_inferred(record: RecordCase, info: DecisionInfo) -> bool:
    """기각 여부를 결정 종류가 아니라 요약 문장에서 읽었다."""
    return info.rejected and outcome_of(record.kind) != "rejected"


def outcome_for(record: RecordCase, info: DecisionInfo) -> Literal["adopted", "rejected"]:
    return "rejected" if info.rejected else outcome_of(record.kind)


def is_candidate(confidence: Confidence, auto_confirm: str = "high") -> bool:
    """자동 확정 확신도가 아니면 후보로 둔다."""
    return confidence != auto_confirm


def _title(record: RecordCase) -> str:
    summary = record.summary.strip().replace("\n", " ")
    short = summary if len(summary) <= 60 else summary[:57] + "..."
    return f"[{LABEL[record.kind]}] {short}"


def _body(record: RecordCase, info: DecisionInfo) -> str:
    alternatives = ", ".join(info.alternatives) if info.alternatives else "없음"
    refs = ", ".join(evidence_refs(record)) or UNKNOWN
    lines = [
        f"결정: {info.decision}",
        f"이유: {info.reason or UNKNOWN}",
        f"검토한 대안: {alternatives}",
        f"근거: {refs}",
    ]
    return "\n".join(lines)


def build_card(
    record: RecordCase,
    info: DecisionInfo,
    now: datetime,
    auto_confirm: str = "high",
    author: str | None = None,
) -> Card:
    """`author`는 토큰으로 확인한 요청자다. 없으면 요청 본문의 값을 쓴다(목·단위 테스트)."""
    who = author or record.author
    confidence = confidence_of(record, info)
    candidate = is_candidate(confidence, auto_confirm)
    area = area_of(record)
    refs = evidence_refs(record)
    outcome = outcome_for(record, info)
    tags = [record.kind.value]
    if area:
        tags.append(f"{AREA_PREFIX}{area}")
    if outcome == "rejected":
        tags.append("rejected")
    if candidate:
        tags.append("candidate")
    return Card(
        card_id=card_id_of(record),
        kind=CARD_KIND[record.kind],
        tenant_id=record.tenant_id,
        context_id=record.context_id,
        title=_title(record),
        body=_body(record, info),
        tags=tags,
        status="candidate" if candidate else "active",
        source_refs=record.source_refs,
        author=who,
        confidence=confidence,
        created_at=now,
        extra={
            "area": area,
            "owner": who,
            "decision": info.decision,
            "reason": info.reason,
            "alternatives_rejected": info.alternatives,
            "decided_at": now.isoformat(),
            "outcome": outcome,
            "outcome_inferred": outcome_inferred(record, info),
            "candidate": candidate,
            "extracted": info.extracted,
            "record_kind": record.kind.value,
            "evidence": {"refs": refs, "source_text_ref": None},
        },
    )
