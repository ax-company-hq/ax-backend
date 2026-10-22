import pytest
from pydantic import ValidationError

from ax_contracts.schemas import (
    EVENT_DATA_MODELS,
    EventType,
    SafetyActionExecutedData,
    SafetyActionHeldData,
    SafetyReviewDecidedData,
    VerifyAmountInput,
    VerifyAmountOutput,
)

SAFETY_EVENT_SAMPLES: dict[EventType, dict] = {
    EventType.SAFETY_ACTION_EXECUTED: {
        "execution_id": "exe_1",
        "action": "issue_coupon",
        "requester": "marketing",
        "grade": "A",
        "computed_amount": 9000,
        "threshold_version": 1,
    },
    EventType.SAFETY_ACTION_HELD: {
        "execution_id": "exe_2",
        "action": "confirm_settlement",
        "requester": "finance",
        "grade": "C",
        "computed_amount": None,
        "threshold_version": 1,
        "hold_reason": "over_threshold",
        "review_id": "rev_1",
    },
    EventType.SAFETY_REVIEW_DECIDED: {
        "review_id": "rev_1",
        "execution_id": "exe_2",
        "action": "confirm_settlement",
        "requester": "finance",
        "decision": "approved",
        "decided_by": "system",
    },
}


@pytest.mark.parametrize("event_type", list(SAFETY_EVENT_SAMPLES))
def test_safety_event_data_models_accept_samples(event_type: EventType) -> None:
    model = EVENT_DATA_MODELS[event_type]
    data = SAFETY_EVENT_SAMPLES[event_type]
    assert model.model_validate(data)
    with pytest.raises(ValidationError):
        model.model_validate({**data, "unknown_field": 1})


def test_safety_event_models_are_the_expected_classes() -> None:
    assert EVENT_DATA_MODELS[EventType.SAFETY_ACTION_EXECUTED] is SafetyActionExecutedData
    assert EVENT_DATA_MODELS[EventType.SAFETY_ACTION_HELD] is SafetyActionHeldData
    assert EVENT_DATA_MODELS[EventType.SAFETY_REVIEW_DECIDED] is SafetyReviewDecidedData


def test_safety_event_models_reject_bad_values() -> None:
    executed = SAFETY_EVENT_SAMPLES[EventType.SAFETY_ACTION_EXECUTED]
    with pytest.raises(ValidationError):
        SafetyActionExecutedData.model_validate({**executed, "grade": "D"})
    with pytest.raises(ValidationError):
        SafetyActionExecutedData.model_validate({**executed, "computed_amount": 100.5})
    held = SAFETY_EVENT_SAMPLES[EventType.SAFETY_ACTION_HELD]
    with pytest.raises(ValidationError):
        SafetyActionHeldData.model_validate({**held, "hold_reason": "because"})
    decided = SAFETY_EVENT_SAMPLES[EventType.SAFETY_REVIEW_DECIDED]
    with pytest.raises(ValidationError):
        SafetyReviewDecidedData.model_validate({**decided, "decision": "held"})


def test_verify_amount_models() -> None:
    inp = VerifyAmountInput.model_validate(
        {"execution_id": "exe_1", "action": "issue_coupon", "requester": "marketing"}
    )
    assert inp.params == {}
    assert inp.computed_amount is None
    out = VerifyAmountOutput.model_validate(
        {"decision": "approved", "reason_code": "recomputed_match", "recomputed": {"total": 9000}}
    )
    assert out.threshold_version is None
    with pytest.raises(ValidationError):
        VerifyAmountInput.model_validate({**inp.model_dump(), "unknown_field": 1})


def test_verify_amount_output_rejects_unknown_reason_code() -> None:
    with pytest.raises(ValidationError):
        VerifyAmountOutput.model_validate({"decision": "approved", "reason_code": "looks_fine"})
    with pytest.raises(ValidationError):
        VerifyAmountOutput.model_validate({"decision": "held", "reason_code": "recomputed_match"})
