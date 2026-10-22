import asyncio
import os
from datetime import timedelta

import pytest

from ax_agent_template import events
from ax_contracts.schemas import EventType
from ax_contracts.schemas.event import MetricAnomalyData
from data_agent import monitor
from data_agent.seed import INCIDENT_FROM, INCIDENT_TO
from data_agent.seed_metrics import MockMetricSource


@pytest.fixture(autouse=True)
def _bus(monkeypatch):
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    os.environ.setdefault("AGENT_NAME", "data")
    events.reset_mock_bus()
    monitor.reset_published()
    yield
    events.reset_mock_bus()
    monitor.reset_published()


def _run(now):
    return asyncio.run(monitor.run_once(MockMetricSource(), now))


def test_incident_emits_one_anomaly():
    out = _run(INCIDENT_TO)
    assert len(out) == 1
    env = out[0]
    assert env.type == EventType.METRIC_ANOMALY
    assert env.source == "data"
    assert env.context_id is None
    assert env.tenant_id == "shop-demo"
    assert env.event_id.startswith("evt_")
    data = MetricAnomalyData.model_validate(env.data)
    assert data.metric == "payment_fail_rate"
    assert data.severity == "high"
    assert data.window_from == INCIDENT_FROM
    assert data.window_to == INCIDENT_TO
    assert data.observed >= data.baseline * 4
    assert env.idempotency_key == f"metric.anomaly:payment_fail_rate:{INCIDENT_FROM.isoformat()}"
    assert events.MOCK_PUBLISHED == out


def test_rerun_does_not_republish():
    first = _run(INCIDENT_TO)
    second = _run(INCIDENT_TO)
    assert len(first) == 1
    assert second == []
    assert len(events.MOCK_PUBLISHED) == 1


def test_normal_time_emits_nothing():
    assert _run(INCIDENT_TO - timedelta(days=1)) == []
    assert events.MOCK_PUBLISHED == []


def test_event_id_is_deterministic():
    a = _run(INCIDENT_TO)[0]
    events.reset_mock_bus()
    monitor.reset_published()
    b = _run(INCIDENT_TO)[0]
    assert a.event_id == b.event_id


def test_growing_window_keeps_same_key_without_republish():
    _run(INCIDENT_FROM + timedelta(minutes=15))
    later = _run(INCIDENT_TO)
    assert later == []
    assert len(events.MOCK_PUBLISHED) == 1
