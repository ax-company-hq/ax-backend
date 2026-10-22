"""쿠폰 계산·순서·중복 처리. 프레임워크에 의존하지 않는다.

- 총액은 건수×단가로 코드가 계산한다(LLM·요청 값을 쓰지 않는다).
- 보상 쿠폰(`compensation`)은 `context_id`별로 `sequence` 1부터 순서대로 한 건씩 처리한다.
  이미 처리한 번호는 이전 결과를 돌려주고, 건너뛴 번호는 `SEQUENCE_GAP`이다.
- 같은 `idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import defaultdict
from typing import Any


def total_value(count: int, unit_value: int) -> int:
    return count * unit_value


class SequenceGap(Exception):
    """기다리는 번호보다 앞선 `sequence`가 들어왔다."""


class CouponLedger:
    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._by_key: dict[str, dict[str, Any]] = {}
        self._done: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)  # context → seq → 결과
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, context_id: str) -> asyncio.Lock:
        """같은 사건의 보상 요청은 한 건씩만 처리하기 위한 잠금."""
        return self._locks[context_id]

    def new_coupon_id(self) -> str:
        return f"cpn_{next(self._counter):04d}"

    def previous_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        return self._by_key.get(idempotency_key)

    def previous_by_sequence(self, context_id: str, sequence: int) -> dict[str, Any] | None:
        return self._done[context_id].get(sequence)

    def check_sequence(self, context_id: str, sequence: int) -> None:
        """처리할 차례가 아니면 `SequenceGap`. 처리한 번호는 호출 전에 `previous_*`로 거른다."""
        expected = len(self._done[context_id]) + 1
        if sequence > expected:
            raise SequenceGap(f"{expected}번을 기다리는데 {sequence}번이 왔다")

    def record(
        self,
        context_id: str,
        idempotency_key: str,
        sequence: int | None,
        result: dict[str, Any],
    ) -> None:
        self._by_key[idempotency_key] = result
        if sequence is not None:
            self._done[context_id][sequence] = result
