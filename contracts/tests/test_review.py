import pytest
from pydantic import ValidationError

from ax_contracts.schemas import ActionReviewInput, ActionReviewOutput
from ax_contracts.schemas.execute import HoldReason


def test_review_input_roundtrip() -> None:
    m = ActionReviewInput(
        execution_id="exe-1",
        action="merge_pr",
        requester="planning",
        params={"pr": 1},
        hold_reason="over_threshold",
        rule_keys=["base_grade"],
    )
    assert ActionReviewInput.model_validate(m.model_dump()) == m
    assert m.hold_reason is HoldReason.OVER_THRESHOLD


def test_review_input_rejects_unknown_hold_reason() -> None:
    with pytest.raises(ValidationError):
        ActionReviewInput(
            execution_id="exe-1", action="merge_pr", requester="planning", hold_reason="nope"
        )


def test_review_output_rejects_bad_decision() -> None:
    with pytest.raises(ValidationError):
        ActionReviewOutput(decision="maybe", reason_code="x", reason_text="y")


def test_review_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ActionReviewOutput(decision="approve", reason_code="x", reason_text="y", reasonCode="z")
