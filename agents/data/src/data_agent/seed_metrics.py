"""주기 감시용 결정적 목 지표 공급자.

2026-10-14 ~ 10-21 7일 정상 이력(시간대별 결정적 패턴)과, 결제 장애 구간
(`seed.INCIDENT_FROM` ~ `INCIDENT_TO`)의 payment_fail_rate 상승을 만든다.
난수를 쓰지 않으므로 같은 입력이면 항상 같은 값이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from data_agent.seed import INCIDENT_FROM, INCIDENT_TO

SERIES_START = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)
SERIES_END = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=5)

# 시간대(UTC 시) 별 기본 수준. 낮에 주문·문의가 많다.
_HOUR_LEVEL = [
    0.3, 0.2, 0.2, 0.2, 0.3, 0.5, 0.8, 1.0, 1.2, 1.4, 1.5, 1.5,
    1.4, 1.3, 1.3, 1.2, 1.2, 1.3, 1.5, 1.6, 1.4, 1.1, 0.8, 0.5,
]  # fmt: skip

# 결정적 변동 계수 (날짜·슬롯 조합으로 순환)
_JITTER = [0.97, 1.0, 1.03, 0.99, 1.02]

# 지표별 기본값 (5분 합계 또는 비율)
_BASE = {
    "payment_fail_rate": 0.02,
    "order_fail_count": 4.0,
    "order_count": 60.0,
    "inquiry_count": 20.0,
}

INCIDENT_MULTIPLIER = 5.0  # 장애 구간 payment_fail_rate 배수


def _jitter(ts: datetime) -> float:
    slot = ts.hour * 12 + ts.minute // 5
    return _JITTER[(ts.toordinal() * 3 + slot) % len(_JITTER)]


def _value(metric: str, ts: datetime) -> float:
    base = _BASE[metric] * _HOUR_LEVEL[ts.hour] * _jitter(ts)
    if metric == "payment_fail_rate":
        if INCIDENT_FROM <= ts < INCIDENT_TO:
            return round(_BASE[metric] * INCIDENT_MULTIPLIER * _jitter(ts), 6)
        return round(base, 6)
    return float(round(base))


class MockMetricSource:
    """`MetricSource` 목 구현. 5분 간격 점을 [window_from, window_to) 범위로 돌려준다."""

    async def fetch(
        self, metric: str, window_from: datetime, window_to: datetime
    ) -> list[tuple[datetime, float]]:
        if metric not in _BASE:
            raise ValueError(f"unknown metric: {metric}")
        ts = max(window_from.astimezone(UTC), SERIES_START)
        # 5분 경계로 올림
        offset = (ts - SERIES_START) % STEP
        if offset:
            ts += STEP - offset
        end = min(window_to.astimezone(UTC), SERIES_END)
        points: list[tuple[datetime, float]] = []
        while ts < end:
            points.append((ts, _value(metric, ts)))
            ts += STEP
        return points
