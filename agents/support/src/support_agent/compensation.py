"""보상 판정과 `sequence` 관리 (DEP-CS-02). 프레임워크에 의존하지 않는다.

- 단가·대상은 코드의 정책표로 판정한다. LLM이나 요청 값을 쓰지 않는다.
- 같은 사건(`context_id`)의 보상 요청은 마케팅 `issue_coupon`에 `sequence` 1부터 한 건씩 보낸다.
- 정책표는 시드다. memory의 정책 문서 API가 생기면 거기서 읽도록 바꾼다.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# 사유 코드 → 쿠폰 1건 단가(원). 표에 없는 사유는 보상 대상이 아니다.
UNIT_VALUE_BY_REASON: dict[str, int] = {
    "late_delivery": 2000,
    "damaged": 3000,
    "wrong_item": 3000,
    "missing_item": 3000,
    "defect": 3000,
}
MAX_COUNT = 3


@dataclass(frozen=True)
class CompensationDecision:
    eligible: bool
    count: int = 0
    unit_value: int = 0
    total_value: int = 0
    reason: str = ""


def decide(reason_code: str, count: int) -> CompensationDecision:
    unit = UNIT_VALUE_BY_REASON.get(reason_code)
    if unit is None:
        return CompensationDecision(False, reason=f"보상 대상이 아닌 사유: {reason_code}")
    if not 1 <= count <= MAX_COUNT:
        return CompensationDecision(False, reason=f"건수는 1~{MAX_COUNT}건이어야 한다")
    return CompensationDecision(True, count, unit, count * unit, "정책표 단가 적용")


class SequenceLedger:
    """사건별 다음 `sequence`와 요청 키별 결과를 기억한다."""

    def __init__(self) -> None:
        self._next: dict[str, int] = defaultdict(lambda: 1)
        self.results: dict[str, dict] = {}

    def peek(self, context_id: str) -> int:
        return self._next[context_id]

    def advance(self, context_id: str) -> None:
        self._next[context_id] += 1
