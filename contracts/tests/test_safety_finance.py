import pytest
from pydantic import ValidationError

from ax_contracts.schemas import (
    EVENT_DATA_MODELS,
    EventType,
    ReviewHoldInput,
    ReviewHoldOutput,
    ReviewThresholdChangeInput,
    ReviewThresholdChangeOutput,
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


def test_review_hold_models_match_what_safety_sends() -> None:
    """safety `call_reviewer`가 보내는 입력과 finance `review_hold`의 출력."""
    sent = {
        "review_id": "rev_1",
        "execution_id": "exe_1",
        "action": "issue_coupon",
        "requester": "marketing",
        "params": {"count": 20, "unit_value": 20_000, "buyer_id": "buy_0001"},
        "computed_amount": 400_000,
        "hold_reason": "over_threshold",
        "rules": [{"key": "coupon_total", "observed": 400_000, "value": 300_000}],
        "totals": {"daily_total": 400_000, "context_total": 400_000, "customer_total": None},
    }
    inp = ReviewHoldInput.model_validate(sent)
    assert inp.model_dump(mode="json") == sent
    with pytest.raises(ValidationError):
        ReviewHoldInput.model_validate({**sent, "unknown_field": 1})
    with pytest.raises(ValidationError):
        ReviewHoldInput.model_validate({k: v for k, v in sent.items() if k != "hold_reason"})

    out = ReviewHoldOutput.model_validate(
        {"decision": "escalate", "reason_code": "over_ai_limit", "explanation": "큰 금액"}
    )
    assert out.decision == "escalate"
    with pytest.raises(ValidationError):
        ReviewHoldOutput.model_validate(
            {"decision": "approved", "reason_code": "x", "explanation": "y"}
        )


def test_review_threshold_change_models_match_what_safety_sends() -> None:
    """safety `_ask_threshold_reviewer`가 보내는 입력과 finance `review_threshold_change`의 출력."""
    sent = {
        "change_id": "thc_0123456789ab",
        "key": "coupon_total",
        "current_value": 300_000,
        "new_value": 350_000,
        "baseline": 300_000,
        "stats": {
            "action": "issue_coupon",
            "holds_24h": 3,
            "executed_today": 120_000,
            "ai_approved_24h": 1,
            "human_approved_24h": 2,
            "expired_24h": 0,
        },
    }
    inp = ReviewThresholdChangeInput.model_validate(sent)
    assert inp.model_dump(mode="json") == sent
    # 마감 기준(review_timeout_seconds)은 action이 없고, 값이 없는 기준은 current_value가 null
    ReviewThresholdChangeInput.model_validate(
        {**sent, "key": "review_timeout_seconds", "current_value": None, "stats": {"action": None}}
    )
    # 요청 이유(untrusted)는 입력에 넣지 않는다
    with pytest.raises(ValidationError):
        ReviewThresholdChangeInput.model_validate({**sent, "reason": "올려 주세요"})
    with pytest.raises(ValidationError):
        ReviewThresholdChangeInput.model_validate(
            {k: v for k, v in sent.items() if k != "new_value"}
        )

    out = ReviewThresholdChangeOutput.model_validate(
        {"decision": "approve", "reason_code": "raise_with_holds", "explanation": "보류 3건"}
    )
    assert out.decision == "approve"
    with pytest.raises(ValidationError):
        ReviewThresholdChangeOutput.model_validate(
            {"decision": "applied", "reason_code": "x", "explanation": "y"}
        )
    with pytest.raises(ValidationError):
        ReviewThresholdChangeOutput.model_validate({"decision": "approve", "reason_code": "x"})
