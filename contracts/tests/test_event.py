import json

import pytest
from pydantic import ValidationError

from ax_contracts.schemas import (
    EVENT_DATA_MODELS,
    CampaignCreatedData,
    CouponIssuedData,
    EventEnvelope,
    EventType,
    IncidentData,
    MetricAnomalyData,
    NoticePublishedData,
    PaymentFailedData,
    ShopDegradedData,
)

ENVELOPE_JSON = json.dumps(
    {
        "event_id": "evt_0001",
        "type": "shop.degraded",
        "occurred_at": "2026-10-22T05:41:00Z",
        "context_id": None,
        "source": "shop-api",
        "idempotency_key": "shop-api:shop.degraded:1",
        "data": {
            "component": "payment",
            "metric_code": "p95_latency_ms",
            "value": 1450.0,
            "threshold": 1000.0,
        },
    }
)


def test_envelope_example_json_validates() -> None:
    event = EventEnvelope.model_validate_json(ENVELOPE_JSON)
    assert event.type == EventType.SHOP_DEGRADED
    assert event.version == 1
    assert event.tenant_id == "shop-demo"
    assert event.untrusted == {}
    assert ShopDegradedData.model_validate(event.data).metric_code == "p95_latency_ms"


def test_unknown_event_type_is_rejected() -> None:
    raw = json.loads(ENVELOPE_JSON)
    raw["type"] = "shop.exploded"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(raw)


def test_event_type_catalog() -> None:
    values = {t.value for t in EventType}
    assert len(values) == 23
    for expected in (
        "order.created",
        "payment.failed",
        "shop.recovered",
        "support.inquiry_received",
        "metric.anomaly",
        "incident.resolved",
        "coupon.issued",
        "safety.review_decided",
    ):
        assert expected in values
    assert set(EVENT_DATA_MODELS) <= set(EventType)


def test_representative_data_models() -> None:
    assert (
        PaymentFailedData.model_validate(
            {"payment_id": "pay_1", "order_id": "ord_1", "amount": 30000, "failure_code": "TIMEOUT"}
        ).amount
        == 30000
    )
    anomaly = MetricAnomalyData.model_validate(
        {
            "metric": "payment_failure_rate",
            "window_from": "2026-10-22T05:00:00Z",
            "window_to": "2026-10-22T05:05:00Z",
            "observed": 0.31,
            "baseline": 0.02,
            "severity": "high",
        }
    )
    assert anomaly.severity == "high"
    incident = IncidentData.model_validate(
        {"component": "payment", "signal_source": "shop", "opened_at": "2026-10-22T05:41:00Z"}
    )
    assert incident.resolved_at is None
    assert NoticePublishedData(notice_id="ntc_1", channel_code="banner", context_id="ctx_inc_1")


def test_amount_must_be_integer_won() -> None:
    with pytest.raises(ValidationError):
        PaymentFailedData.model_validate(
            {"payment_id": "p", "order_id": "o", "amount": 100.5, "failure_code": "X"}
        )


def test_campaign_with_promises() -> None:
    campaign = CampaignCreatedData.model_validate(
        {
            "campaign_id": "cmp_1",
            "target_codes": ["BOOK"],
            "starts_at": "2026-10-22T00:00:00Z",
            "ends_at": "2026-10-25T00:00:00Z",
            "promises": [
                {
                    "type": "points",
                    "amount": 1000,
                    "condition_code": "LATE_3D",
                    "condition": "3일 지연",
                }
            ],
        }
    )
    assert campaign.promises[0].condition_code == "LATE_3D"


def test_coupon_purpose_and_campaign_id() -> None:
    compensation = CouponIssuedData(
        coupon_id="cpn_1", purpose_code="compensation", count=3, total_value=9000
    )
    assert compensation.campaign_id is None
    with pytest.raises(ValidationError, match="campaign_id"):
        CouponIssuedData(
            coupon_id="cpn_1",
            campaign_id="cmp_1",
            purpose_code="compensation",
            count=1,
            total_value=1000,
        )
    with pytest.raises(ValidationError, match="campaign_id"):
        CouponIssuedData(coupon_id="cpn_1", purpose_code="campaign", count=1, total_value=1000)
