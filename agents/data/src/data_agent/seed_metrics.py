"""주기 감시용 결정적 목 지표 공급자.

2026-10-14 ~ 10-21 7일 정상 이력(시간대별 결정적 패턴)과, 결제 장애 구간
(`seed.INCIDENT_FROM` ~ `INCIDENT_TO`)의 payment_fail_rate 상승을 만든다.
난수를 쓰지 않으므로 같은 입력이면 항상 같은 값이다.

데모 정렬: 분석 구간이 `seed.DEMO_ALIGN_FROM` 이후면(시연처럼 지금 시각을 물으면) 날짜 제한 없이
같은 시간대 패턴으로 점을 만들고(7일 기준선), 장애를 [anchor, anchor + 장애 길이)에 둔다.
anchor는 impact_analysis가 정하고 지표는 읽기만 한다(`demo_anchor`). anchor가 없으면 정상 값이다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from data_agent import demo_anchor
from data_agent.metrics.anomaly import BASELINE_DAYS
from data_agent.seed import DEMO_ALIGN_FROM, INCIDENT_FROM, INCIDENT_TO

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


def _value(
    metric: str,
    ts: datetime,
    incident: tuple[datetime, datetime] = (INCIDENT_FROM, INCIDENT_TO),
) -> float:
    base = _BASE[metric] * _HOUR_LEVEL[ts.hour] * _jitter(ts)
    if metric == "payment_fail_rate":
        if incident[0] <= ts < incident[1]:
            return round(_BASE[metric] * INCIDENT_MULTIPLIER * _jitter(ts), 6)
        return round(base, 6)
    return float(round(base))


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _ceil_step(ts: datetime) -> datetime:
    offset = (ts - _EPOCH) % STEP
    return ts + (STEP - offset) if offset else ts


def _analysis_start(window_from: datetime, window_to: datetime) -> datetime:
    """기준선까지 한 번에 가져오는 요청(`anomaly_detection`, `monitor`의 이력 조회)은 범위가
    7일보다 길어서 시작 + 7일이 분석 구간 시작이다."""
    baseline = timedelta(days=BASELINE_DAYS)
    return window_from + baseline if window_to - window_from > baseline else window_from


def demo_incident(
    window_from: datetime, window_to: datetime, *, starts_incident: bool = False
) -> tuple[datetime, datetime] | None:
    """데모 정렬 때의 장애 구간 [anchor, anchor + 장애 길이).

    분석 구간이 `DEMO_ALIGN_FROM` 전이면 None(고정 시드 그대로)이다. 기본은 저장된 anchor를 읽기만
    하고, 없으면 장애가 없다(빈 구간). `starts_incident=True`면 impact_analysis와 같은 규칙으로
    분석 구간 시작을 anchor로 정한다(지표만으로 시연·테스트할 때).
    """
    start = _analysis_start(window_from, window_to)
    if start < DEMO_ALIGN_FROM:
        return None
    anchor = demo_anchor.anchor_for(start, window_to) if starts_incident else demo_anchor.current()
    if anchor is None:
        return (start, start)  # 빈 구간: 장애 없음
    return anchor, anchor + demo_anchor.incident_length()


class MockMetricSource:
    """`MetricSource` 목 구현. 5분 간격 점을 [window_from, window_to) 범위로 돌려준다.

    기본(`starts_incident=False`)은 데모 anchor를 만들거나 갱신하지 않고 읽기만 한다. anchor는
    impact_analysis가 정하므로 감시 루프나 복구 확인이 장애를 새로 만들지 않는다.
    `starts_incident=True`면 지표 요청도 anchor를 정한다(지표만으로 시연·테스트할 때).
    """

    def __init__(self, *, starts_incident: bool = False) -> None:
        self.starts_incident = starts_incident

    async def fetch(
        self, metric: str, window_from: datetime, window_to: datetime
    ) -> list[tuple[datetime, float]]:
        if metric not in _BASE:
            raise ValueError(f"unknown metric: {metric}")
        window_from, window_to = window_from.astimezone(UTC), window_to.astimezone(UTC)
        incident = demo_incident(window_from, window_to, starts_incident=self.starts_incident)
        if incident is None:  # 고정 날짜 시드
            ts = _ceil_step(max(window_from, SERIES_START))
            end = min(window_to, SERIES_END)
            incident = (INCIDENT_FROM, INCIDENT_TO)
        else:  # 데모 정렬
            ts, end = _ceil_step(window_from), window_to
        points: list[tuple[datetime, float]] = []
        while ts < end:
            points.append((ts, _value(metric, ts, incident)))
            ts += STEP
        return points
