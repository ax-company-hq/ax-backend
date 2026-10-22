"""업무 지표 이상 판정 계산 (anomaly_detection).

5분 버킷 집계, 지난 7일 같은 시간대 기준선, 3σ/N배 판정, severity를 순수 계산으로 낸다.
LLM을 쓰지 않으며 같은 입력이면 항상 같은 결과를 낸다.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

Point = tuple[datetime, float]
Agg = Literal["sum", "mean"]
Severity = Literal["low", "mid", "high"]

BUCKET_SECONDS = 300
BASELINE_DAYS = 7
MIN_SAMPLES = 3  # 기준선에 필요한 최소 표본 수

DEFAULT_SIGMA = 3.0
DEFAULT_RATIO_THRESHOLD = 3.0

# severity 임계값
HIGH_RATIO = 4.0
HIGH_Z = 6.0
MID_RATIO = 2.0
MID_Z = 4.0


@dataclass(frozen=True)
class Baseline:
    mean: float
    std: float
    sample_count: int


@dataclass(frozen=True)
class Judgement:
    is_anomaly: bool
    ratio: float | None
    z: float | None
    severity: Severity | None


@dataclass(frozen=True)
class BucketResult:
    bucket_ts: datetime
    observed: float
    baseline_mean: float | None
    baseline_std: float | None
    ratio: float | None
    z: float | None
    is_anomaly: bool
    severity: Severity | None
    judged: bool  # 기준선이 없으면 False (판정 불가, 이상 아님)


def _to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime is not allowed (UTC aware required)")
    return ts.astimezone(UTC)


def _floor_5m(ts: datetime) -> datetime:
    ts = _to_utc(ts)
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


def bucket_5m(points: list[Point], agg: Agg = "sum") -> list[Point]:
    """점들을 UTC 5분 경계 버킷으로 집계해 시각 오름차순으로 돌려준다."""
    if agg not in ("sum", "mean"):
        raise ValueError(f"unknown agg: {agg}")
    groups: dict[datetime, list[float]] = {}
    for ts, value in points:
        groups.setdefault(_floor_5m(ts), []).append(float(value))
    out: list[Point] = []
    for key in sorted(groups):
        vals = groups[key]
        out.append((key, sum(vals) if agg == "sum" else sum(vals) / len(vals)))
    return out


def baseline_for(bucket_ts: datetime, history: list[Point]) -> Baseline | None:
    """지난 7일 같은 시간대(시:분) 값들의 평균·표준편차를 구한다.

    history는 5분 버킷 점 목록이다. 표본이 3개 미만이면 None(기준선 없음).
    """
    target = _to_utc(bucket_ts)
    start = target - timedelta(days=BASELINE_DAYS)
    values: list[float] = []
    for ts, value in history:
        t = _to_utc(ts)
        if start <= t < target and (t.hour, t.minute) == (target.hour, target.minute):
            values.append(float(value))
    if len(values) < MIN_SAMPLES:
        return None
    return Baseline(
        mean=statistics.fmean(values),
        std=statistics.pstdev(values),
        sample_count=len(values),
    )


def judge(
    observed: float,
    mean: float,
    std: float,
    sigma: float = DEFAULT_SIGMA,
    ratio_threshold: float = DEFAULT_RATIO_THRESHOLD,
) -> Judgement:
    """관측값이 기준선 대비 이상인지 판정한다 (상승 방향만).

    ratio는 mean이 0이면 None, z는 std가 0이면 None이다.
    둘 다 None이면 비교 근거가 없으므로 이상이 아니다.
    """
    ratio = observed / mean if mean > 0 else None
    z = (observed - mean) / std if std > 0 else None
    is_anomaly = (z is not None and z > sigma) or (ratio is not None and ratio > ratio_threshold)
    if not is_anomaly:
        return Judgement(False, ratio, z, None)
    if (ratio is not None and ratio >= HIGH_RATIO) or (z is not None and z >= HIGH_Z):
        severity: Severity = "high"
    elif (ratio is not None and ratio >= MID_RATIO) or (z is not None and z >= MID_Z):
        severity = "mid"
    else:
        severity = "low"
    return Judgement(True, ratio, z, severity)


def detect(
    series: list[Point],
    window_from: datetime,
    window_to: datetime,
    history: list[Point],
    agg: Agg = "sum",
) -> list[BucketResult]:
    """구간 [window_from, window_to) 의 5분 버킷마다 이상 여부를 판정한다.

    series·history는 원시 점이며 같은 agg로 5분 버킷 집계한다.
    """
    start = _to_utc(window_from)
    end = _to_utc(window_to)
    hist = bucket_5m(history, agg)
    results: list[BucketResult] = []
    for bucket_ts, observed in bucket_5m(series, agg):
        if not (start <= bucket_ts < end):
            continue
        base = baseline_for(bucket_ts, hist)
        if base is None:
            results.append(
                BucketResult(bucket_ts, observed, None, None, None, None, False, None, False)
            )
            continue
        j = judge(observed, base.mean, base.std)
        results.append(
            BucketResult(
                bucket_ts,
                observed,
                base.mean,
                base.std,
                j.ratio,
                j.z,
                j.is_anomaly,
                j.severity,
                True,
            )
        )
    return results
