"""업무 지표 주기 감시 (DEP-DATA-01).

지표별로 최근 구간과 지난 7일 이력을 가져와 `detect`로 이상 버킷을 찾고, 연속된 이상 버킷을
하나의 구간으로 묶어 `metric.anomaly` 이벤트를 발행한다. 주기 실행이므로 LLM을 부르지 않는다
(요청 예산). 사건(context_id)은 통합 에이전트만 열기 때문에 이벤트의 context_id는 None이다.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ax_agent_template.events import publish
from ax_contracts.schemas import EventEnvelope, EventType
from ax_contracts.schemas.event import MetricAnomalyData
from data_agent.metrics.anomaly import BASELINE_DAYS, BUCKET_SECONDS, Agg, BucketResult, detect

Point = tuple[datetime, float]

# (지표, 5분 버킷 집계 방식)
WATCHED_METRICS: list[tuple[str, Agg]] = [
    ("payment_fail_rate", "mean"),
    ("order_fail_count", "sum"),
    ("order_count", "sum"),
    ("inquiry_count", "sum"),
]

_BUCKET = timedelta(seconds=BUCKET_SECONDS)
_SEVERITY_RANK = {"low": 0, "mid": 1, "high": 2}

# 프로세스 안에서 이미 발행한 idempotency_key
_published_keys: set[str] = set()


class MetricSource(Protocol):
    """지표 시계열 공급자.

    TODO: shop-api 지표 시계열 계약(PR #72)이 머지되면 그 클라이언트로 교체한다.
    """

    async def fetch(
        self, metric: str, window_from: datetime, window_to: datetime
    ) -> list[Point]: ...


def reset_published() -> None:
    """발행 기록을 비운다 (테스트용)."""
    _published_keys.clear()


def _group_consecutive(anomalies: list[BucketResult]) -> list[list[BucketResult]]:
    groups: list[list[BucketResult]] = []
    for r in sorted(anomalies, key=lambda x: x.bucket_ts):
        if groups and r.bucket_ts - groups[-1][-1].bucket_ts == _BUCKET:
            groups[-1].append(r)
        else:
            groups.append([r])
    return groups


def _envelope(metric: str, group: list[BucketResult], now: datetime) -> EventEnvelope:
    window_from = group[0].bucket_ts
    window_to = group[-1].bucket_ts + _BUCKET
    peak = max(group, key=lambda r: r.observed)
    severity = max((r.severity for r in group if r.severity), key=_SEVERITY_RANK.__getitem__)
    data = MetricAnomalyData(
        metric=metric,
        window_from=window_from,
        window_to=window_to,
        observed=peak.observed,
        baseline=peak.baseline_mean if peak.baseline_mean is not None else 0.0,
        severity=severity,
    )
    key = f"metric.anomaly:{metric}:{window_from.isoformat()}"
    return EventEnvelope(
        event_id="evt_" + hashlib.sha256(key.encode()).hexdigest()[:16],
        type=EventType.METRIC_ANOMALY,
        occurred_at=now,
        context_id=None,
        source="data",
        idempotency_key=key,
        data=data.model_dump(mode="json"),
        untrusted={},
    )


async def run_once(
    source: MetricSource,
    now: datetime,
    *,
    lookback: timedelta = timedelta(minutes=30),
) -> list[EventEnvelope]:
    """한 번 감시해 새로 발행한 `metric.anomaly` 이벤트를 돌려준다."""
    now = now.astimezone(UTC)
    window_from = now - lookback
    emitted: list[EventEnvelope] = []
    for metric, agg in WATCHED_METRICS:
        series = await source.fetch(metric, window_from, now)
        history = await source.fetch(metric, window_from - timedelta(days=BASELINE_DAYS), now)
        results = detect(series, window_from, now, history, agg)
        for group in _group_consecutive([r for r in results if r.is_anomaly]):
            env = _envelope(metric, group, now)
            if env.idempotency_key in _published_keys:
                continue
            _published_keys.add(env.idempotency_key)
            await publish(env)
            emitted.append(env)
    return emitted


async def run_forever(
    source: MetricSource,
    interval_seconds: int = 300,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """`interval_seconds` 마다 `run_once`를 반복한다."""
    while True:
        await run_once(source, clock())
        await asyncio.sleep(interval_seconds)
