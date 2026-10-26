"""목 모드 데모 시각 정렬 테스트.

시연은 지금 시각으로 사건을 열고 총괄이 사건 시각 ±15분으로 impact_analysis를 부른다. 고정 날짜
시드(2026-10-21)만 있으면 0건이 나오므로, `DEMO_ALIGN_FROM` 이후 구간을 물으면 목 shop·지표가 시드
장애를 요청 구간으로 옮긴다. 고정 날짜 테스트는 다른 파일에 그대로 있다.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from ax_agent_template import events
from ax_agent_template.llm import MockLLMClient
from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersQuery
from ax_contracts.schemas.data import AnomalyDetectionInput, ImpactAnalysisInput
from data_agent import demo_anchor, monitor, seed
from data_agent.clients.shop_client import MockShopClient, demo_shift
from data_agent.impact import aggregate_impact
from data_agent.metrics.anomaly import BASELINE_DAYS
from data_agent.seed_metrics import MockMetricSource, demo_incident
from data_agent.skills.anomaly_detection import detect_anomalies
from data_agent.skills.impact_analysis import analyze_impact

# 사건 시각: 5분 경계가 아닌 임의의 시연 시각
OPENED_AT = datetime(2026, 10, 26, 3, 12, 34, tzinfo=UTC)
WINDOW_FROM = OPENED_AT - timedelta(minutes=15)  # 총괄 scenario_a `_window`와 같은 방식
WINDOW_TO = OPENED_AT + timedelta(minutes=15)
ENV = {"DATA_MOCK": "1"}
TENANT = "shop-demo"


def _orders(window_from: datetime, window_to: datetime, category: str | None = None):
    query = ShopFailedOrdersQuery(from_=window_from, to=window_to, category=category)
    return asyncio.run(MockShopClient().list_failed_orders(query))


# ---------- shop 실패 주문 ----------


def test_shift_is_zero_before_align_time() -> None:
    assert demo_shift(seed.INCIDENT_FROM) == timedelta(0)
    assert demo_shift(seed.DEMO_ALIGN_FROM - timedelta(seconds=1)) == timedelta(0)
    assert demo_shift(WINDOW_FROM) == WINDOW_FROM - seed.INCIDENT_FROM


def test_demo_window_matches_seed_answer() -> None:
    counts = aggregate_impact(_orders(WINDOW_FROM, WINDOW_TO))
    assert counts.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT
    assert counts.affected_customer_count == seed.EXPECTED_AFFECTED_CUSTOMER_COUNT
    assert counts.damage_amount == seed.EXPECTED_DAMAGE_AMOUNT


def test_demo_window_books_matches_seed_answer() -> None:
    counts = aggregate_impact(_orders(WINDOW_FROM, WINDOW_TO, category="books"))
    assert counts.failed_order_count == seed.EXPECTED_BOOKS_ORDER_COUNT
    assert counts.affected_customer_count == seed.EXPECTED_BOOKS_CUSTOMER_COUNT
    assert counts.damage_amount == seed.EXPECTED_BOOKS_DAMAGE_AMOUNT


def test_demo_orders_are_moved_into_window() -> None:
    result = _orders(WINDOW_FROM, WINDOW_TO)
    assert all(WINDOW_FROM <= o.failed_at <= WINDOW_TO for o in result.orders)
    assert {o.order_id for o in result.orders} == {f"ord_seed00{i}" for i in range(1, 7)}


def test_demo_trap_orders_keep_relative_position() -> None:
    """구간 밖 함정 3건은 옮긴 뒤에도 장애 시작 기준 −5분, +45분, +90분에 있다."""
    wide = _orders(WINDOW_FROM, WINDOW_FROM + timedelta(hours=3))
    by_id = {o.order_id: o.failed_at - WINDOW_FROM for o in wide.orders}
    assert "ord_seed007" not in by_id  # −5분이라 구간 시작보다 앞
    assert by_id["ord_seed008"] == timedelta(minutes=45)
    assert by_id["ord_seed009"] == timedelta(minutes=90)
    assert len(wide.orders) == len(seed.SEED_FAILED_ORDERS) - 1  # 장애 6건 + 뒤쪽 함정 2건


def test_demo_original_seed_is_not_mutated() -> None:
    _orders(WINDOW_FROM, WINDOW_TO)
    assert seed.SEED_FAILED_ORDERS[0].failed_at.date() == seed.INCIDENT_FROM.date()


def test_impact_skill_on_demo_window() -> None:
    data = ImpactAnalysisInput(window_from=WINDOW_FROM, window_to=WINDOW_TO, scope="payment")
    out = asyncio.run(analyze_impact(data, TENANT, MockLLMClient(), ENV)).output
    assert out.failed_order_count == seed.EXPECTED_FAILED_ORDER_COUNT
    assert out.damage_amount == seed.EXPECTED_DAMAGE_AMOUNT
    assert out.affected_customer_ids == ["buy_0001", "buy_0002", "buy_0003", "buy_0004"]


# ---------- 지표 시계열 ----------


def test_metric_incident_is_none_before_align_time() -> None:
    start = seed.INCIDENT_FROM
    history_from = start - timedelta(days=BASELINE_DAYS)
    assert demo_incident(history_from, start + timedelta(minutes=30)) is None


def test_metric_incident_anchors_on_analysis_start() -> None:
    """기준선까지 한 번에 가져와도(7일 넘는 범위) 장애는 분석 구간 시작에 놓인다."""
    start = datetime(2026, 10, 26, 2, 55, tzinfo=UTC)
    short = demo_incident(start, start + timedelta(minutes=30), starts_incident=True)
    long = demo_incident(
        start - timedelta(days=BASELINE_DAYS), start + timedelta(minutes=30), starts_incident=True
    )
    assert short == long == (start, start + demo_anchor.incident_length())


def test_metric_series_has_seven_day_baseline() -> None:
    start = datetime(2026, 10, 26, 2, 55, tzinfo=UTC)
    history_from = start - timedelta(days=BASELINE_DAYS)
    source = MockMetricSource(starts_incident=True)
    points = asyncio.run(
        source.fetch("payment_fail_rate", history_from, start + timedelta(minutes=30))
    )
    values = dict(points)
    assert points[0][0] == start - timedelta(days=BASELINE_DAYS)
    assert len(points) == BASELINE_DAYS * 288 + 6  # 5분 간격 7일 + 분석 구간 30분
    for day in range(1, BASELINE_DAYS + 1):  # 지난 7일 같은 시각은 정상 수준
        assert values[start - timedelta(days=day)] < values[start]


def test_anomaly_detection_on_demo_window() -> None:
    data = AnomalyDetectionInput(window_from=WINDOW_FROM, window_to=WINDOW_TO)
    result = asyncio.run(
        detect_anomalies(data, MockMetricSource(starts_incident=True), MockLLMClient())
    ).output
    verdicts = {v.metric: v for v in result.verdicts}
    assert result.any_anomaly is True
    assert verdicts["payment_fail_rate"].is_anomaly is True
    assert verdicts["payment_fail_rate"].baseline > 0
    assert [m for m, v in verdicts.items() if v.is_anomaly] == ["payment_fail_rate"]


@pytest.fixture
def _bus(monkeypatch):
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    os.environ.setdefault("AGENT_NAME", "data")
    events.reset_mock_bus()
    monitor.reset_published()
    yield
    events.reset_mock_bus()
    monitor.reset_published()


@pytest.mark.usefixtures("_bus")
def test_monitor_emits_one_anomaly_on_demo_time() -> None:
    """감시는 최근 구간과 7일 이력을 따로 가져오지만 장애 위치가 같아 이벤트 1건만 낸다."""
    out = asyncio.run(monitor.run_once(MockMetricSource(starts_incident=True), OPENED_AT))
    assert len(out) == 1
    assert out[0].data["metric"] == "payment_fail_rate"
