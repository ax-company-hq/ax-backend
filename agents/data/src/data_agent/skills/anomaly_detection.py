"""anomaly_detection skill (DEP-DATA-01, 이슈 #55).

구간 [window_from, window_to)의 5분 버킷을 지난 7일 같은 시간대 기준선과 비교해 지표별
판정을 낸다. 숫자·판정은 코드(`metrics.anomaly.detect`)가 만들고, LLM은 문장만 쓴다.
infra `verify_recovery`가 롤백 후 구간을 물어 `any_anomaly`로 복구 여부를 확인한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ax_agent_template.llm import LLMClient
from ax_contracts.schemas import Evidence
from ax_contracts.schemas.data import AnomalyDetectionInput, AnomalyDetectionOutput, MetricVerdict
from data_agent.clients.shop_client import HttpMetricSource, shop_api
from data_agent.explain import explain
from data_agent.metrics.anomaly import BASELINE_DAYS, BucketResult, detect
from data_agent.monitor import WATCHED_METRICS, MetricSource
from data_agent.seed_metrics import MockMetricSource

_SEVERITY_RANK = {"low": 0, "mid": 1, "high": 2}
_AGG = dict(WATCHED_METRICS)
_DIGITS = 4


@dataclass(frozen=True)
class AnomalyResult:
    output: AnomalyDetectionOutput
    evidence: Evidence
    fallback: bool


def get_metric_source(env: Mapping[str, str]) -> MetricSource:
    """`DATA_MOCK=1`이면 목 공급자, 아니면 shop-api `/api/shop/metric-series` 공급자."""
    if env.get("DATA_MOCK") == "1":
        return MockMetricSource()
    return HttpMetricSource(shop_api(env))


def _floor_5m(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


def _verdict(metric: str, results: list[BucketResult]) -> MetricVerdict:
    """버킷 판정들을 지표 하나의 판정으로 모은다.

    이상 버킷이 있으면 그 중 관측값이 가장 큰 버킷이 대표이고 severity는 최고 값이다.
    없으면 기준선이 있는 버킷 중 관측값이 가장 큰 것이 대표다. 기준선 있는 버킷이 없으면
    판정 불가라서 이상이 아니다(baseline 0).
    """
    judged = [r for r in results if r.judged]
    anomalies = [r for r in judged if r.is_anomaly]
    if not judged:
        observed = max((r.observed for r in results), default=0.0)
        return MetricVerdict(
            metric=metric,  # type: ignore[arg-type]
            observed=round(observed, _DIGITS),
            baseline=0.0,
            ratio=None,
            is_anomaly=False,
            severity=None,
        )
    peak = max(anomalies or judged, key=lambda r: r.observed)
    severity = (
        max((r.severity for r in anomalies if r.severity), key=_SEVERITY_RANK.__getitem__)
        if anomalies
        else None
    )
    return MetricVerdict(
        metric=metric,  # type: ignore[arg-type]
        observed=round(peak.observed, _DIGITS),
        baseline=round(peak.baseline_mean or 0.0, _DIGITS),
        ratio=round(peak.ratio, 2) if peak.ratio is not None else None,
        is_anomaly=bool(anomalies),
        severity=severity,
    )


def _worst(verdicts: list[MetricVerdict]) -> MetricVerdict:
    return max(
        verdicts,
        key=lambda v: (_SEVERITY_RANK[v.severity or "low"], v.ratio or 0.0),
    )


async def detect_anomalies(
    data: AnomalyDetectionInput, source: MetricSource, llm: LLMClient
) -> AnomalyResult:
    metrics = list(dict.fromkeys(data.metrics)) or [name for name, _ in WATCHED_METRICS]
    start = _floor_5m(data.window_from)
    verdicts: list[MetricVerdict] = []
    for metric in metrics:
        history = await source.fetch(metric, start - timedelta(days=BASELINE_DAYS), data.window_to)
        series = [(ts, value) for ts, value in history if ts >= start]
        results = detect(series, data.window_from, data.window_to, history, _AGG[metric])
        verdicts.append(_verdict(metric, results))

    abnormal = [v for v in verdicts if v.is_anomaly]
    any_anomaly = bool(abnormal)
    fallback = False
    if any_anomaly:
        worst = _worst(abnormal)
        explanation = await explain(
            "anomaly",
            {
                "metric": worst.metric,
                "baseline": worst.baseline,
                "observed": worst.observed,
                "ratio": worst.ratio if worst.ratio is not None else "-",
            },
            llm,
        )
        summary, fallback = explanation.summary, explanation.fallback
    else:  # 이상이 없으면 설명할 숫자가 없어서 LLM 없이 고정 문장을 쓴다
        summary = "구간 안에서 기준선을 벗어난 지표가 없습니다."

    output = AnomalyDetectionOutput(verdicts=verdicts, any_anomaly=any_anomaly, summary=summary)
    evidence = Evidence(
        queries=[
            f"metric_series metrics={','.join(metrics)} "
            f"from={data.window_from.isoformat()} to={data.window_to.isoformat()} "
            f"baseline_days={BASELINE_DAYS}"
        ],
        data_summary=(
            f"지표 {len(verdicts)}개 중 이상 {len(abnormal)}개"
            + (f" ({', '.join(v.metric for v in abnormal)})" if abnormal else "")
        ),
        collected_at=datetime.now(UTC),
    )
    return AnomalyResult(output=output, evidence=evidence, fallback=fallback)
