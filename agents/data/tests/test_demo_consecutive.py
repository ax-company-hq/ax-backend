"""SCN-A 인수 기준 "재시도 없이 연속 3회 성공": 같은 프로세스에서 사건을 연달아 주입한다.

anchor는 impact_analysis만 정한다. 구간 시작이 저장된 anchor와 1분 이내면 같은 사건의 재조회라
anchor를 유지하고, 그 밖에는(구간이 겹쳐도) 새 사건이라 anchor를 새로 잡는다. 지표는 읽기만 한다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ax_agent_template.llm import MockLLMClient
from ax_contracts.schemas.data import AnomalyDetectionInput, ImpactAnalysisInput
from data_agent import demo_anchor, seed
from data_agent.seed_metrics import MockMetricSource
from data_agent.skills.anomaly_detection import detect_anomalies
from data_agent.skills.impact_analysis import analyze_impact

FIRST_OPENED_AT = datetime(2026, 10, 26, 3, 12, 34, tzinfo=UTC)
GAP = timedelta(minutes=10)  # 사건 사이 간격
ENV = {"DATA_MOCK": "1"}
TENANT = "shop-demo"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock(FIRST_OPENED_AT)
    monkeypatch.setattr(demo_anchor, "clock", fake)
    return fake


def _window(opened_at: datetime) -> tuple[datetime, datetime]:
    """총괄 scenario_a `_window`: 사건 시각 ±15분."""
    return opened_at - timedelta(minutes=15), opened_at + timedelta(minutes=15)


def _impact(window_from: datetime, window_to: datetime):
    data = ImpactAnalysisInput(window_from=window_from, window_to=window_to, scope="payment")
    return asyncio.run(analyze_impact(data, TENANT, MockLLMClient(), ENV)).output


def _anomaly(window_from: datetime, window_to: datetime):
    data = AnomalyDetectionInput(window_from=window_from, window_to=window_to)
    return asyncio.run(detect_anomalies(data, MockMetricSource(), MockLLMClient())).output


def _payment(output) -> bool:
    return next(v for v in output.verdicts if v.metric == "payment_fail_rate").is_anomaly


def test_three_consecutive_incidents_in_one_process(clock: _Clock) -> None:
    """사건 주입 → impact 6건 → 복구 확인 정상, 10분 간격으로 3회 연속."""
    for run in range(3):
        opened_at = FIRST_OPENED_AT + GAP * run
        clock.now = opened_at
        window_from, window_to = _window(opened_at)

        impact = _impact(window_from, window_to)
        assert demo_anchor.current() == window_from, f"{run + 1}회차 anchor"
        assert (
            impact.failed_order_count,
            impact.affected_customer_count,
            impact.damage_amount,
        ) == (
            seed.EXPECTED_FAILED_ORDER_COUNT,
            seed.EXPECTED_AFFECTED_CUSTOMER_COUNT,
            seed.EXPECTED_DAMAGE_AMOUNT,
        ), f"{run + 1}회차 impact"

        assert _payment(_anomaly(window_from, window_to)) is True, f"{run + 1}회차 장애 구간"

        clock.now = opened_at + timedelta(minutes=2)  # 롤백 직후 복구 확인 (since = 사건 시각)
        recovered = _anomaly(opened_at, clock.now)
        assert recovered.any_anomaly is False, f"{run + 1}회차 복구 확인"


def test_anomaly_without_anchor_is_normal(clock: _Clock) -> None:
    """impact 없이 anomaly_detection만 부르면 anchor가 없어 정상 값이다."""
    window_from, window_to = _window(FIRST_OPENED_AT)
    out = _anomaly(window_from, window_to)
    assert out.any_anomaly is False
    assert all(v.baseline > 0 for v in out.verdicts)  # 7일 기준선은 있다
    assert demo_anchor.current() is None  # 지표는 anchor를 만들지 않는다


def test_same_incident_window_twice_keeps_anchor(clock: _Clock) -> None:
    window_from, window_to = _window(FIRST_OPENED_AT)
    _impact(window_from, window_to)
    first = demo_anchor.current()
    clock.now += timedelta(minutes=3)
    again = _impact(window_from, window_to)  # 재무 손실 리포트 같은 같은 사건 재조회
    assert demo_anchor.current() == first == window_from
    assert again.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT


def test_overlapping_window_keeps_anchor(clock: _Clock) -> None:
    """구간 시작이 anchor와 1분 이내인 재조회는 같은 사건이라 anchor를 바꾸지 않고 6건 그대로다.

    (새 규칙: 같은 사건 판정은 "장애와 겹침"이 아니라 "구간 시작이 1분 이내"다. 예전 입력인
    장애 끝 1분 전 시작 구간은 이제 새 사건이며
    `test_overlap_with_different_start_is_new_incident`가 확인한다.)
    """
    window_from, window_to = _window(FIRST_OPENED_AT)
    _impact(window_from, window_to)
    late = timedelta(seconds=40)  # 허용 오차 안에서 늦게 시작한 재조회
    again = _impact(window_from + late, window_to + late)
    assert demo_anchor.current() == window_from
    assert again.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT


def test_overlap_with_different_start_is_new_incident(clock: _Clock) -> None:
    """장애와 겹쳐도 구간 시작이 1분 넘게 다르면 새 사건이다 (e2e가 쉬지 않고 다시 주입한 경우)."""
    window_from, window_to = _window(FIRST_OPENED_AT)
    _impact(window_from, window_to)
    end = window_from + demo_anchor.incident_length()
    out = _impact(end - timedelta(minutes=1), end + timedelta(minutes=30))
    assert demo_anchor.current() == end - timedelta(minutes=1)
    assert out.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT


def test_runs_one_minute_apart_each_get_full_impact(clock: _Clock) -> None:
    """e2e `--runs 3` 재현: 쉬지 않고 1분 간격으로 "그 순간 ±15분" impact 3회 → 매번 같은 정답."""
    results = []
    for run in range(3):
        clock.now = FIRST_OPENED_AT + timedelta(minutes=run)
        window_from, window_to = _window(clock.now)
        out = _impact(window_from, window_to)
        # 정확히 1분 간격은 허용 오차 경계라 같은 사건으로 남을 수 있다. 어느 쪽이든 정답은 같다
        anchor = demo_anchor.current()
        assert anchor is not None
        assert abs(anchor - window_from) <= demo_anchor.SAME_INCIDENT_TOLERANCE, f"{run + 1}회차"
        results.append((out.failed_order_count, out.affected_customer_count, out.damage_amount))
    expected = (
        seed.EXPECTED_FAILED_ORDER_COUNT,
        seed.EXPECTED_AFFECTED_CUSTOMER_COUNT,
        seed.EXPECTED_DAMAGE_AMOUNT,
    )
    assert results == [expected] * 3  # judge.merge_runs: 회차 결과가 같아야 통과


def test_requery_within_tolerance_keeps_all_orders(clock: _Clock) -> None:
    """같은 사건 재조회가 허용 오차 끝(1분)만큼 늦어도 장애 주문이 빠지지 않는다."""
    window_from, window_to = _window(FIRST_OPENED_AT)
    first = _impact(window_from, window_to)
    tol = demo_anchor.SAME_INCIDENT_TOLERANCE
    late = _impact(window_from + tol, window_to + tol)
    early = _impact(window_from - tol, window_to - tol)
    assert demo_anchor.current() == window_from
    assert first == late == early


def test_window_after_incident_end_is_new_incident(clock: _Clock) -> None:
    window_from, window_to = _window(FIRST_OPENED_AT)
    _impact(window_from, window_to)
    end = window_from + demo_anchor.incident_length()
    out = _impact(end, end + timedelta(minutes=30))  # 장애 끝 정각부터: 겹치지 않음
    assert demo_anchor.current() == end
    assert out.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT


def test_reset_is_still_a_safety_net(clock: _Clock) -> None:
    window_from, window_to = _window(FIRST_OPENED_AT)
    _impact(window_from, window_to)
    clock.now += demo_anchor.reset_after()
    assert demo_anchor.current() is None
