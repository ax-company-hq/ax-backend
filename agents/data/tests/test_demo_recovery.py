"""목 모드 데모 장애 기억(anchor) 테스트: 장애 주입 → 롤백 → 복구 확인.

총괄은 사건 시각 ±15분으로 impact_analysis를 부르고, infra `verify_recovery`는 롤백 뒤 구간으로
anomaly_detection을 부른다. 목이 처음 정렬 요청의 구간 시작을 장애 시작(anchor)으로 기억해야
롤백 뒤 구간에서 정상 지표가 나와 복구 확인을 통과한다.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from ax_agent_template import events
from ax_agent_template.llm import MockLLMClient
from ax_contracts.schemas.data import AnomalyDetectionInput, ImpactAnalysisInput
from data_agent import demo_anchor, monitor, seed
from data_agent.seed_metrics import MockMetricSource
from data_agent.skills.anomaly_detection import detect_anomalies
from data_agent.skills.impact_analysis import analyze_impact

OPENED_AT = datetime(2026, 10, 26, 3, 12, 34, tzinfo=UTC)
WINDOW_FROM = OPENED_AT - timedelta(minutes=15)  # 총괄 scenario_a `_window`
WINDOW_TO = OPENED_AT + timedelta(minutes=15)
ENV = {"DATA_MOCK": "1"}
TENANT = "shop-demo"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock(OPENED_AT)
    monkeypatch.setattr(demo_anchor, "clock", fake)
    return fake


def _impact(window_from: datetime = WINDOW_FROM, window_to: datetime = WINDOW_TO):
    data = ImpactAnalysisInput(window_from=window_from, window_to=window_to, scope="payment")
    return asyncio.run(analyze_impact(data, TENANT, MockLLMClient(), ENV)).output


def _anomaly(window_from: datetime, window_to: datetime):
    data = AnomalyDetectionInput(window_from=window_from, window_to=window_to)
    return asyncio.run(detect_anomalies(data, MockMetricSource(), MockLLMClient())).output


def _payment(output) -> bool:
    return next(v for v in output.verdicts if v.metric == "payment_fail_rate").is_anomaly


# ---------- 장애 중 / 복구 뒤 ----------


@pytest.mark.usefixtures("clock")
def test_incident_window_is_anomaly() -> None:
    impact = _impact()
    assert (impact.failed_order_count, impact.affected_customer_count, impact.damage_amount) == (
        seed.EXPECTED_FAILED_ORDER_COUNT,
        seed.EXPECTED_AFFECTED_CUSTOMER_COUNT,
        seed.EXPECTED_DAMAGE_AMOUNT,
    )
    out = _anomaly(WINDOW_FROM, WINDOW_TO)
    assert out.any_anomaly is True
    assert _payment(out) is True


@pytest.mark.usefixtures("clock")
def test_anchor_is_first_window_start() -> None:
    _impact()
    assert demo_anchor.current() == WINDOW_FROM
    _anomaly(OPENED_AT, OPENED_AT + timedelta(minutes=5))  # 뒤 요청은 anchor를 바꾸지 않는다
    assert demo_anchor.current() == WINDOW_FROM


@pytest.mark.usefixtures("clock")
def test_after_incident_payment_rate_is_normal() -> None:
    """anchor + 장애 길이 이후 구간은 결제 실패율이 정상이라 복구 확인을 통과한다."""
    _impact()
    end = WINDOW_FROM + demo_anchor.incident_length()
    after = _anomaly(end, end + timedelta(minutes=10))  # 장애가 끝나는 정각부터
    assert _payment(after) is False
    assert after.any_anomaly is False
    # verify_recovery 계약 기준(since = 사건 시각)의 구간도 정상
    since = _anomaly(OPENED_AT, OPENED_AT + timedelta(minutes=5))
    assert since.any_anomaly is False


@pytest.mark.usefixtures("clock")
def test_impact_after_rollback_keeps_seed_answer() -> None:
    """복구 확인 뒤 같은 사건 구간을 다시 물어도(재무 손실 리포트 등) 정답이 같다."""
    _impact()
    _anomaly(OPENED_AT, OPENED_AT + timedelta(minutes=5))
    again = _impact()
    assert again.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT
    assert again.damage_amount == seed.EXPECTED_DAMAGE_AMOUNT
    assert again.affected_customer_ids == ["buy_0001", "buy_0002", "buy_0003", "buy_0004"]


@pytest.mark.usefixtures("clock")
def test_demo_orders_fit_in_incident_length() -> None:
    from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersQuery
    from data_agent.clients.shop_client import MockShopClient

    query = ShopFailedOrdersQuery(from_=WINDOW_FROM, to=WINDOW_TO)
    orders = asyncio.run(MockShopClient().list_failed_orders(query)).orders
    end = WINDOW_FROM + demo_anchor.incident_length()
    assert len(orders) == seed.EXPECTED_FAILED_ORDER_COUNT
    assert all(WINDOW_FROM <= o.failed_at < end for o in orders)


# ---------- 결정성 ----------


@pytest.mark.usefixtures("clock")
def test_same_window_twice_gives_same_result() -> None:
    first = (_impact(), _anomaly(WINDOW_FROM, WINDOW_TO))
    after = (OPENED_AT, OPENED_AT + timedelta(minutes=5))
    first_after = _anomaly(*after)
    second = (_impact(), _anomaly(WINDOW_FROM, WINDOW_TO))
    assert first == second
    assert _anomaly(*after) == first_after


# ---------- 리셋 ----------


@pytest.mark.usefixtures("clock")
def test_reset_starts_new_anchor() -> None:
    _impact()
    demo_anchor.reset_demo_anchor()
    later = WINDOW_FROM + timedelta(hours=2)
    _impact(later, later + timedelta(minutes=30))
    assert demo_anchor.current() == later


def test_anchor_expires_after_idle(clock: _Clock) -> None:
    _impact()
    # 마지막 anchor 요청 뒤 59분: 유지 (지표 읽기는 시각을 갱신하지 않는다)
    clock.now += timedelta(minutes=59)
    _anomaly(OPENED_AT, OPENED_AT + timedelta(minutes=5))
    assert demo_anchor.current() == WINDOW_FROM
    clock.now += timedelta(minutes=60)  # 마지막 anchor 요청 뒤 60분 넘음: 만료
    assert demo_anchor.current() is None
    new_from = WINDOW_FROM + timedelta(hours=2)
    out = _impact(new_from, new_from + timedelta(minutes=30))
    assert demo_anchor.current() == new_from
    assert out.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT


def test_reset_minutes_from_env(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DEMO_RESET_MINUTES", "10")
    _impact()
    clock.now += timedelta(minutes=10)
    assert demo_anchor.current() is None


# ---------- 장애 길이 설정 ----------


@pytest.mark.usefixtures("clock")
def test_incident_minutes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DEMO_INCIDENT_MINUTES", "20")
    assert demo_anchor.incident_length() == timedelta(minutes=20)
    assert _impact().failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT
    still = _anomaly(WINDOW_FROM + timedelta(minutes=10), WINDOW_FROM + timedelta(minutes=15))
    assert _payment(still) is True


def test_incident_minutes_default_and_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    assert demo_anchor.incident_length() == timedelta(minutes=6)
    monkeypatch.setenv("DATA_DEMO_INCIDENT_MINUTES", "2")
    assert demo_anchor.incident_length() == timedelta(minutes=5)


# ---------- 감시 루프 ----------


@pytest.fixture
def _bus(monkeypatch):
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    os.environ.setdefault("AGENT_NAME", "data")
    events.reset_mock_bus()
    monitor.reset_published()
    yield
    events.reset_mock_bus()
    monitor.reset_published()


@pytest.mark.usefixtures("_bus", "clock")
def test_monitor_repeated_runs_emit_one_event() -> None:
    """5분마다 돌아도 같은 장애라 새 이벤트는 처음 한 번뿐이다."""
    _impact()
    source = MockMetricSource(starts_incident=False)
    emitted = []
    for minutes in range(0, 60, 5):
        emitted += asyncio.run(monitor.run_once(source, OPENED_AT + timedelta(minutes=minutes)))
    assert len(emitted) == 1
    assert emitted[0].data["metric"] == "payment_fail_rate"


@pytest.mark.usefixtures("_bus", "clock")
def test_monitor_without_demo_request_makes_no_incident() -> None:
    source = MockMetricSource(starts_incident=False)
    for minutes in range(0, 30, 5):
        assert asyncio.run(monitor.run_once(source, OPENED_AT + timedelta(minutes=minutes))) == []
    assert demo_anchor.current() is None
