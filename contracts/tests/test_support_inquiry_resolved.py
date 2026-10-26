import pytest
from pydantic import ValidationError

from ax_contracts.schemas.event import (
    EVENT_DATA_MODELS,
    EventType,
    SupportInquiryResolvedData,
)


def test_event_type_registered_with_data_model():
    assert EventType.SUPPORT_INQUIRY_RESOLVED == "support.inquiry_resolved"
    assert EVENT_DATA_MODELS[EventType.SUPPORT_INQUIRY_RESOLVED] is SupportInquiryResolvedData


def test_compensated_resolution_roundtrip():
    data = SupportInquiryResolvedData.model_validate(
        {
            "inquiry_id": 7,
            "order_id": "ord_1",
            "status": "resolved",
            "resolution": "compensated",
            "coupon_id": "cpn_1",
            "refund_amount": 3000,
        }
    )
    assert data.claim_id is None
    assert SupportInquiryResolvedData.model_validate(data.model_dump(mode="json")) == data


def test_minimal_escalated_resolution():
    data = SupportInquiryResolvedData.model_validate(
        {"inquiry_id": 8, "status": "escalated", "resolution": "escalated"}
    )
    assert data.order_id is None
    assert data.claim_decision is None


@pytest.mark.parametrize(
    "over",
    [
        {"status": "closed"},
        {"resolution": "refunded"},
        {"claim_decision": "maybe"},
        {"refund_amount": -1},
        {"customer_text": "raw"},
    ],
)
def test_invalid_values_rejected(over):
    base = {"inquiry_id": 1, "status": "resolved", "resolution": "answered"}
    with pytest.raises(ValidationError):
        SupportInquiryResolvedData.model_validate({**base, **over})
