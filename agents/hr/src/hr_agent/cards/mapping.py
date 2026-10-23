"""`RecordCase.kind` → 카드 종류·표시 이름 매핑표 (인사 구성안 HR2 3장, 결정 19).

카드 종류는 계약 `Card.kind`(`incident`·`decision`·`case`·`notice`·`policy`·`fraud`)다.
결정이 들어 있는 기록(`DECISION_KINDS`)은 결정 카드가 되고, 그 밖의 기록은 결정 이유가 있을 때만
카드가 된다. 결정 이유가 없으면 기록만 남긴다(`record_only`).
"""

from __future__ import annotations

from typing import Literal

from ax_contracts.schemas import RecordKind

CardKind = Literal["incident", "decision", "case", "notice", "policy", "fraud"]

CARD_KIND: dict[RecordKind, CardKind] = {
    RecordKind.INCIDENT: "incident",
    RecordKind.DECISION: "decision",
    RecordKind.CASE: "case",
    RecordKind.NOTICE: "notice",
    RecordKind.ROLLBACK_EXECUTED: "decision",  # 롤백은 이유·대안이 있는 결정이다
    RecordKind.SPEC_CONFIRMED: "decision",
    RecordKind.PR_MERGED: "case",
    RecordKind.DECISION_EXTRACTED: "decision",
    RecordKind.REQUEST_CLOSED: "case",
    RecordKind.DECISION_REJECTED: "decision",  # 기각한 안. `extra.outcome = "rejected"`
}

# 기록 자체가 결정이라서 이유가 없어도 카드를 만든다(확신도는 낮게, 후보로).
DECISION_KINDS: frozenset[RecordKind] = frozenset(
    {
        RecordKind.DECISION,
        RecordKind.SPEC_CONFIRMED,
        RecordKind.DECISION_EXTRACTED,
        RecordKind.DECISION_REJECTED,
        RecordKind.ROLLBACK_EXECUTED,
    }
)

LABEL: dict[RecordKind, str] = {
    RecordKind.INCIDENT: "사건",
    RecordKind.DECISION: "결정",
    RecordKind.CASE: "사례",
    RecordKind.NOTICE: "공지",
    RecordKind.ROLLBACK_EXECUTED: "롤백 실행",
    RecordKind.SPEC_CONFIRMED: "기획 확정",
    RecordKind.PR_MERGED: "PR 머지",
    RecordKind.DECISION_EXTRACTED: "결정 추출",
    RecordKind.REQUEST_CLOSED: "요청 종료",
    RecordKind.DECISION_REJECTED: "기각한 안",
}


def outcome_of(kind: RecordKind) -> Literal["adopted", "rejected"]:
    return "rejected" if kind == RecordKind.DECISION_REJECTED else "adopted"
