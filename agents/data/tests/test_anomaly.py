from datetime import UTC, datetime, timedelta

import pytest

from data_agent.metrics.anomaly import baseline_for, bucket_5m, detect, judge

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)  # 판정 대상 날짜의 12:00


def _history(noise: bool = True) -> list[tuple[datetime, float]]:
    """T0 기준 지난 7일, 11:55~12:30 구간 5분 값 (평균 약 100)."""
    points = []
    for day in range(1, 8):
        for k in range(-1, 7):
            ts = T0 - timedelta(days=day) + timedelta(minutes=5 * k)
            points.append((ts, 100.0 + (day % 3 if noise else 0)))
    return points


def test_bucket_sum_and_mean_with_boundary():
    pts = [
        (datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC), 1.0),
        (datetime(2026, 10, 8, 12, 4, 59, tzinfo=UTC), 3.0),
        (datetime(2026, 10, 8, 12, 5, 0, tzinfo=UTC), 10.0),
    ]
    assert bucket_5m(pts, "sum") == [
        (datetime(2026, 10, 8, 12, 0, tzinfo=UTC), 4.0),
        (datetime(2026, 10, 8, 12, 5, tzinfo=UTC), 10.0),
    ]
    assert bucket_5m(pts, "mean")[0][1] == 2.0


def test_bucket_rejects_naive():
    with pytest.raises(ValueError):
        bucket_5m([(datetime(2026, 10, 8, 12, 0), 1.0)])


def test_outage_four_times_is_high_and_normal_is_clean():
    history = _history()
    series = [(T0 + timedelta(minutes=5 * k), 100.0) for k in range(0, 3)]
    series += [(T0 + timedelta(minutes=5 * k), 400.0) for k in range(3, 5)]
    results = detect(series, T0, T0 + timedelta(minutes=25), history)
    assert len(results) == 5
    for r in results[:3]:
        assert r.judged and not r.is_anomaly and r.severity is None
    for r in results[3:]:
        assert r.is_anomaly and r.severity == "high"
        assert r.ratio is not None and r.ratio >= 3.9


def test_deterministic():
    history = _history()
    series = [(T0 + timedelta(minutes=5 * k), 150.0 + k) for k in range(5)]
    end = T0 + timedelta(minutes=25)
    assert detect(series, T0, end, history) == detect(series, T0, end, history)


def test_insufficient_history_not_judged():
    history = _history()[:2]
    results = detect([(T0, 1000.0)], T0, T0 + timedelta(minutes=5), history)
    assert len(results) == 1
    r = results[0]
    assert not r.judged and not r.is_anomaly and r.baseline_mean is None
    assert baseline_for(T0, history) is None


def test_baseline_only_same_time_within_7_days():
    history = _history() + [(T0 - timedelta(days=8), 9999.0)]
    base = baseline_for(T0, history)
    assert base is not None and base.sample_count == 7 and base.mean < 200


def test_judge_std_zero_and_mean_zero():
    j = judge(500.0, 100.0, 0.0)  # std 0: z 없음, ratio 5 -> 이상
    assert j.z is None and j.is_anomaly and j.severity == "high"
    assert not judge(100.0, 100.0, 0.0).is_anomaly
    j0 = judge(10.0, 0.0, 0.0)  # mean 0, std 0: 근거 없음
    assert j0.ratio is None and j0.z is None and not j0.is_anomaly
    jz = judge(10.0, 0.0, 2.0)  # mean 0: ratio 없음, z 5 -> 이상 mid
    assert jz.ratio is None and jz.is_anomaly and jz.severity == "mid"


def test_judge_severity_levels():
    assert judge(350.0, 100.0, 1000.0).severity == "mid"  # ratio 3.5 이상, mid 미만 z
    assert judge(450.0, 100.0, 1000.0).severity == "high"
    assert judge(110.0, 100.0, 2.0).severity == "mid"  # z 5, ratio 1.1
    assert judge(110.0, 100.0, 1.5).severity == "high"  # z 6.67
    assert judge(106.0, 100.0, 1.5).severity == "mid"  # z 4
    assert judge(105.0, 100.0, 1.5).severity == "low"  # z 3.33
