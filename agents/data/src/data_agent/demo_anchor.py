"""목 모드 데모 장애 시각(anchor)을 프로세스 안에서 기억한다.

`DEMO_ALIGN_FROM` 이후 구간을 처음 물으면 그 구간 시작을 장애 시작으로 저장하고, 이후 요청은
실패 주문·지표 모두 같은 anchor를 쓴다. 그래서 롤백 뒤 구간을 물으면 장애가 끝난 정상 지표가
나온다(복구 확인 통과). 마지막 요청 뒤 `DATA_DEMO_RESET_MINUTES`가 지나면 anchor를 비운다.

- `DATA_DEMO_INCIDENT_MINUTES`: 장애 길이(분). 기본 6, 최소 5(5분 간격 지표 점이 하나는 들어가게)
- `DATA_DEMO_RESET_MINUTES`: 마지막 요청 뒤 anchor를 비우는 시간(분). 기본 60
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

DEFAULT_INCIDENT_MINUTES = 6
MIN_INCIDENT_MINUTES = 5
DEFAULT_RESET_MINUTES = 60

_anchor: datetime | None = None
_last_seen: datetime | None = None


def _clock() -> datetime:
    return datetime.now(UTC)


# 테스트가 바꿔 끼우는 시계
clock: Callable[[], datetime] = _clock


def _minutes(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def incident_length() -> timedelta:
    minutes = _minutes("DATA_DEMO_INCIDENT_MINUTES", DEFAULT_INCIDENT_MINUTES)
    return timedelta(minutes=max(minutes, MIN_INCIDENT_MINUTES))


def reset_after() -> timedelta:
    return timedelta(minutes=_minutes("DATA_DEMO_RESET_MINUTES", DEFAULT_RESET_MINUTES))


def _expired(now: datetime) -> bool:
    return _last_seen is not None and now - _last_seen >= reset_after()


def anchor_for(candidate: datetime) -> datetime:
    """저장된 anchor를 돌려준다. 없거나 만료됐으면 `candidate`를 새 anchor로 저장한다."""
    global _anchor, _last_seen
    now = clock()
    if _anchor is None or _expired(now):
        _anchor = candidate
    _last_seen = now
    return _anchor


def current() -> datetime | None:
    """anchor를 만들거나 갱신하지 않고 읽기만 한다 (감시 루프용). 만료됐으면 None."""
    if _anchor is None or _expired(clock()):
        return None
    return _anchor


def reset_demo_anchor() -> None:
    """anchor를 비운다 (테스트·리허설용)."""
    global _anchor, _last_seen
    _anchor = None
    _last_seen = None
