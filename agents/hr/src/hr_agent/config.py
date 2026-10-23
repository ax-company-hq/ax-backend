"""hr 설정: 환경변수 읽기, 목/실제 전환, 시계.

환경변수는 `HR_` 접두사 대문자다. 기획안에서 값이 정해지지 않은 항목(`(TODO: 추후결정)`)은
제안값으로 시작하고 환경변수로 바꿀 수 있게 둔다.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]

TRUE_VALUES = ("1", "true", "yes", "on")


def system_clock() -> datetime:
    return datetime.now(UTC)


class ManualClock:
    """테스트·데모용 시계. 값을 직접 정하고 앞으로 돌릴 수 있어서 결과가 매번 같다."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in TRUE_VALUES


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class HrSettings:
    """hr 동작 설정.

    - `mock`: memory·planning·안전 레이어를 목으로 대체한다 (`HR_MOCK=1`, CI는 항상 1).
    - `db_path`: hr이 소유하는 SQLite 파일 (기본은 메모리).
    - `handover_min_completeness`: 인수인계 확정 완성도 기준(B/C 경계). 기획안 미정, 제안값 80.
    - `handover_question_ttl_minutes`: 인계자 질문에 답이 없을 때 "미응답"으로 보는 시간.
    - `hold_ttl_minutes`: C 보류 건을 자동 거절하기까지 시간(데모 기준 10분).
    - `card_auto_confirm`: 이 확신도일 때만 카드를 자동 확정한다. 기획안은 high만, 세부 기준 미정.
    - `planning_mode`·`memory_mode`: `mock`(기본) 또는 실제 연동(`a2a`·`http`, `clients/real.py`).
    - `lock_ttl_minutes`: 변경 잠금을 잡고 있는 요청이 이 시간 넘게 끝나지 않으면 잠금을 푼다.
    - `similarity_threshold`: 계획 비교에서 "관련 카드"로 보는 유사도 하한(제안값).
    """

    mock: bool = True
    db_path: str = ":memory:"
    handover_min_completeness: int = 80
    handover_question_ttl_minutes: int = 60
    hold_ttl_minutes: int = 10
    card_auto_confirm: str = "high"
    similarity_threshold: float = 0.2
    timer_enabled: bool = False
    lock_ttl_minutes: int = 1440
    planning_mode: str = "mock"
    memory_mode: str = "mock"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HrSettings:
        source = os.environ if env is None else env
        return cls(
            mock=_flag(source, "HR_MOCK") or _flag(source, "AGENT_TEMPLATE_MOCK"),
            db_path=source.get("HR_DB_PATH") or ":memory:",
            handover_min_completeness=_int(source, "HR_HANDOVER_MIN_COMPLETENESS", 80),
            handover_question_ttl_minutes=_int(source, "HR_HANDOVER_QUESTION_TTL_MIN", 60),
            hold_ttl_minutes=_int(source, "HR_HOLD_TTL_MIN", 10),
            card_auto_confirm=source.get("HR_CARD_AUTO_CONFIRM") or "high",
            timer_enabled=_flag(source, "HR_TIMER"),
            lock_ttl_minutes=_int(source, "HR_LOCK_TTL_MIN", 1440),
            planning_mode=source.get("HR_PLANNING_MODE") or "mock",
            memory_mode=source.get("HR_MEMORY_MODE") or "mock",
        )
