"""시계. 서비스는 시각을 직접 읽지 않고 주입받은 시계만 쓴다 (테스트에서 고정하기 위해).

`StepClock`은 프로토타입의 `now()`와 같다: 10-22 10:42(KST)에서 시작해 부를 때마다 1분씩 간다.
목 모드의 기본 시계이고, 실제 운영에서는 `system_clock`을 넘긴다.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
DEMO_YEAR = 2026

Clock = Callable[[], datetime]


def kst(stamp: str, year: int = DEMO_YEAR) -> datetime:
    """프로토타입 표시 시각 `"10-21 14:04"`(KST)를 UTC aware datetime으로 바꾼다."""
    naive = datetime.strptime(f"{year}-{stamp}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=KST).astimezone(UTC)


class StepClock:
    """부를 때마다 `step`만큼 가는 결정적 시계."""

    def __init__(self, start: datetime | None = None, step: timedelta = timedelta(minutes=1)):
        self._start = start or kst("10-22 10:42")
        self._step = step
        self._calls = 0

    def __call__(self) -> datetime:
        value = self._start + self._step * self._calls
        self._calls += 1
        return value


def system_clock() -> datetime:
    return datetime.now(UTC)


def default_clock_factory() -> Callable[[], Clock]:
    """`PLANNING_CLOCK=real`이면 실제 시각, 아니면 결정적 `StepClock`.

    총괄 혼합 모드처럼 다른 부서가 지금 시각 구간으로 묻는 시연에서는 실제 시각이어야
    시뮬레이터로 머지한 PR이 장애 구간에 잡힌다. 테스트·기본 목 모드는 그대로 StepClock.
    """
    if os.getenv("PLANNING_CLOCK", "").strip().lower() == "real":
        return lambda: system_clock
    return StepClock
