"""impact_analysis·anomaly_detection skill 테스트 (LLM은 mock, 네트워크 없음)."""

from datetime import UTC, datetime

import pytest

from ax_agent_template.llm import MockLLMClient
from ax_contracts.schemas.data import AnomalyDetectionInput, ImpactAnalysisInput
from data_agent.explain import _Summary
from data_agent.seed import (
    EXPECTED_AFFECTED_CUSTOMER_COUNT,
    EXPECTED_DAMAGE_AMOUNT,
    EXPECTED_FAILED_ORDER_COUNT,
    INCIDENT_FROM,
    INCIDENT_TO,
)
from data_agent.seed_metrics import MockMetricSource
from data_agent.skills.anomaly_detection import detect_anomalies, get_metric_source
from data_agent.skills.impact_analysis import analyze_impact

pytestmark = pytest.mark.asyncio

ENV = {"DATA_MOCK": "1"}
TENANT = "shop-demo"


def _llm(text: str | None = None) -> MockLLMClient:
    # 응답을 등록하지 않으면 KeyError가 나고 explain이 템플릿(fallback=True)으로 대체한다
    return MockLLMClient(responses={_Summary: _Summary(summary=text)} if text else {})


def _impact_input(**kw) -> ImpactAnalysisInput:
    return ImpactAnalysisInput(window_from=INCIDENT_FROM, window_to=INCIDENT_TO, **kw)


async def test_impact_matches_seed_answer() -> None:
    text = "실패 주문 6건, 영향 고객 4명, 피해 금액 95,000원입니다."
    result = await analyze_impact(_impact_input(), TENANT, _llm(text), ENV)
    out = result.output
    assert out.failed_order_count == EXPECTED_FAILED_ORDER_COUNT
    assert out.affected_customer_count == EXPECTED_AFFECTED_CUSTOMER_COUNT
    assert out.damage_amount == EXPECTED_DAMAGE_AMOUNT
    assert out.affected_customer_ids == ["buy_0001", "buy_0002", "buy_0003", "buy_0004"]
    assert out.summary == text
    assert result.fallback is False


async def test_impact_category_distribution_sums_to_total() -> None:
    out = (await analyze_impact(_impact_input(), TENANT, _llm(), ENV)).output
    assert [c.category for c in out.by_category] == ["books", "stationery"]
    assert sum(c.failed_order_count for c in out.by_category) == out.failed_order_count
    assert sum(c.damage_amount for c in out.by_category) == out.damage_amount
    books = out.by_category[0]
    assert (books.failed_order_count, books.damage_amount) == (4, 82500)


async def test_impact_category_filter() -> None:
    out = (await analyze_impact(_impact_input(category="books"), TENANT, _llm(), ENV)).output
    assert out.failed_order_count == 4
    assert out.damage_amount == 82500
    assert [c.category for c in out.by_category] == ["books"]


async def test_impact_evidence() -> None:
    result = await analyze_impact(_impact_input(category="books"), TENANT, _llm(), ENV)
    query = result.evidence.queries[0]
    assert query.startswith("shop_failed_orders from=2026-10-21T05:00:00")
    assert "category=books" in query
    assert "95000" not in result.evidence.data_summary  # books만이라 82500
    assert "82500" in result.evidence.data_summary
    assert result.evidence.collected_at is not None


async def test_impact_is_deterministic() -> None:
    first = await analyze_impact(_impact_input(), TENANT, _llm(), ENV)
    second = await analyze_impact(_impact_input(), TENANT, _llm(), ENV)
    assert first.output == second.output
    assert first.fallback == second.fallback


async def test_impact_empty_window() -> None:
    data = ImpactAnalysisInput(
        window_from=datetime(2026, 10, 1, tzinfo=UTC), window_to=datetime(2026, 10, 2, tzinfo=UTC)
    )
    out = (await analyze_impact(data, TENANT, _llm(), ENV)).output
    assert (out.failed_order_count, out.affected_customer_count, out.damage_amount) == (0, 0, 0)
    assert out.by_category == []


async def test_impact_wrong_llm_number_falls_back_keeping_numbers() -> None:
    result = await analyze_impact(_impact_input(), TENANT, _llm("실패 주문 7건입니다."), ENV)
    assert result.fallback is True
    assert result.output.failed_order_count == EXPECTED_FAILED_ORDER_COUNT
    assert result.output.damage_amount == EXPECTED_DAMAGE_AMOUNT
    assert "실패 주문 6건" in result.output.summary


async def test_impact_requires_mock_env() -> None:
    with pytest.raises(NotImplementedError):
        await analyze_impact(_impact_input(), TENANT, _llm(), {})


# ---------- anomaly_detection ----------


def _anomaly_input(start: datetime, end: datetime, **kw) -> AnomalyDetectionInput:
    return AnomalyDetectionInput(window_from=start, window_to=end, **kw)


def _verdicts(output) -> dict:
    return {v.metric: v for v in output.verdicts}


async def test_anomaly_during_incident() -> None:
    data = _anomaly_input(INCIDENT_FROM, INCIDENT_TO)
    result = await detect_anomalies(data, MockMetricSource(), _llm())
    verdicts = _verdicts(result.output)
    assert set(verdicts) == {
        "payment_fail_rate",
        "order_fail_count",
        "order_count",
        "inquiry_count",
    }
    pay = verdicts["payment_fail_rate"]
    assert pay.is_anomaly is True
    assert pay.severity == "high"
    assert pay.observed > pay.baseline > 0
    assert pay.ratio is not None and pay.ratio > 3
    assert result.output.any_anomaly is True
    assert result.evidence.queries and result.evidence.collected_at is not None
    assert result.fallback is True  # LLM 응답 미등록 -> 템플릿


async def test_anomaly_summary_from_llm_when_numbers_match() -> None:
    data = _anomaly_input(INCIDENT_FROM, INCIDENT_TO, metrics=["payment_fail_rate"])
    first = await detect_anomalies(data, MockMetricSource(), _llm())
    pay = first.output.verdicts[0]
    text = f"payment_fail_rate가 기준선 {pay.baseline} 대비 {pay.observed}로 {pay.ratio}배입니다."
    result = await detect_anomalies(data, MockMetricSource(), _llm(text))
    assert result.fallback is False
    assert result.output.summary == text


async def test_anomaly_selected_metrics_only() -> None:
    data = _anomaly_input(INCIDENT_FROM, INCIDENT_TO, metrics=["payment_fail_rate"])
    result = await detect_anomalies(data, MockMetricSource(), _llm())
    assert [v.metric for v in result.output.verdicts] == ["payment_fail_rate"]


async def test_no_anomaly_after_recovery_same_day() -> None:
    data = _anomaly_input(INCIDENT_TO, datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    result = await detect_anomalies(data, MockMetricSource(), _llm())
    assert result.output.any_anomaly is False
    assert all(v.is_anomaly is False and v.severity is None for v in result.output.verdicts)
    assert result.fallback is False


async def test_no_anomaly_same_time_day_before() -> None:
    start = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
    result = await detect_anomalies(
        _anomaly_input(start, datetime(2026, 10, 20, 5, 30, tzinfo=UTC)), MockMetricSource(), _llm()
    )
    assert result.output.any_anomaly is False
    assert all(v.baseline > 0 for v in result.output.verdicts if v.metric != "payment_fail_rate")


async def test_no_data_window_is_not_anomaly() -> None:
    """이력이 없는 구간(판정 불가)은 이상이 아니다."""
    start = datetime(2026, 10, 22, 5, 0, tzinfo=UTC)
    result = await detect_anomalies(
        _anomaly_input(start, datetime(2026, 10, 22, 5, 30, tzinfo=UTC)), MockMetricSource(), _llm()
    )
    assert result.output.any_anomaly is False
    assert all(v.baseline == 0.0 and v.ratio is None for v in result.output.verdicts)


async def test_anomaly_is_deterministic() -> None:
    data = _anomaly_input(INCIDENT_FROM, INCIDENT_TO)
    first = await detect_anomalies(data, MockMetricSource(), _llm())
    second = await detect_anomalies(data, MockMetricSource(), _llm())
    assert first.output == second.output


async def test_get_metric_source() -> None:
    assert isinstance(get_metric_source(ENV), MockMetricSource)
    with pytest.raises(NotImplementedError):
        get_metric_source({})
