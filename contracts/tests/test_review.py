import pytest
from pydantic import ValidationError

from ax_contracts.schemas import ActionReviewInput, ActionReviewOutput


def test_review_input_roundtrip() -> None:
    m = ActionReviewInput(
        execution_id="exe-1",
        action="merge_pr",
        requester="planning",
        params={"pr": 1},
        hold_reasons=["over_threshold"],
    )
    assert ActionReviewInput.model_validate(m.model_dump()) == m


def test_review_output_rejects_bad_decision() -> None:
    with pytest.raises(ValidationError):
        ActionReviewOutput(decision="maybe", reason_code="x", reason_text="y")


def test_review_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ActionReviewOutput(decision="approve", reason_code="x", reason_text="y", reasonCode="z")
