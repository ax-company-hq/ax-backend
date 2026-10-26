"""목 모드 데모 장애 시각(anchor)을 프로세스 안에서 기억한다.

anchor는 impact_analysis(목 shop 정렬 경로)만 정한다. 지표는 읽기만 한다.
- 저장된 anchor가 없거나, 요청 구간 [from, to)가 저장된 장애 [anchor, anchor + 길이)와 겹치지 않고
  요청 구간 시작이 장애 끝 이후면 새 사건으로 보고 anchor = 요청 구간 시작으로 새로 잡는다.
- 겹치면 같은 사건의 재조회라 기존 anchor를 유지한다.
그래서 롤백 뒤 구간을 물으면 장애가 끝난 정상 지표가 나오고(복구 확인 통과), 같은 프로세스에서
사건을 연달아 주입해도 매번 새 장애가 잡힌다. 안전장치로 마지막 anchor 요청 뒤
`DATA_DEMO_RESET_MINUTES`가 지나면 anchor를 비운다.

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


def _is_new_incident(window_from: datetime, window_to: datetime) -> bool:
    """요청 구간이 저장된 장애와 겹치지 않고 장애 끝 이후에 시작하면 새 사건이다."""
    assert _anchor is not None
    end = _anchor + incident_length()
    overlaps = window_from < end and window_to > _anchor
    return not overlaps and window_from >= end


def anchor_for(window_from: datetime, window_to: datetime | None = None) -> datetime:
    """anchor를 정하고 돌려준다. 없거나 만료됐거나 새 사건이면 `window_from`을 새 anchor로 저장한다.

    `window_to`가 없으면 시각 하나([from, from])로 본다.
    """
    global _anchor, _last_seen
    now = clock()
    window_to = window_from if window_to is None else window_to
    if _anchor is None or _expired(now) or _is_new_incident(window_from, window_to):
        _anchor = window_from
    _last_seen = now
    return _anchor


def current() -> datetime | None:
    """anchor를 만들거나 갱신하지 않고 읽기만 한다 (지표 기본 동작). 없거나 만료됐으면 None."""
    if _anchor is None or _expired(clock()):
        return None
    return _anchor


def reset_demo_anchor() -> None:
    """anchor를 비운다 (테스트·리허설용)."""
    global _anchor, _last_seen
    _anchor = None
    _last_seen = None
