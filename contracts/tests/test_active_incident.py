import pytest
from pydantic import ValidationError

from ax_contracts.schemas import FindActiveIncidentInput, FindActiveIncidentOutput


def test_find_active_incident_input_is_all_optional() -> None:
    assert FindActiveIncidentInput.model_validate({}).component is None
    ask = FindActiveIncidentInput.model_validate(
        {"component": "payment", "occurred_at": "2026-10-16T01:00:00Z"}
    )
    assert ask.component == "payment"
    with pytest.raises(ValidationError):
        FindActiveIncidentInput.model_validate({"order_id": "ord_1"})


def test_found_needs_context_id() -> None:
    hit = FindActiveIncidentOutput.model_validate(
        {
            "found": True,
            "context_id": "ctx_inc_0001",
            "component": "payment",
            "status": "open",
            "opened_at": "2026-10-16T00:45:00Z",
        }
    )
    assert hit.closed_at is None
    with pytest.raises(ValidationError):
        FindActiveIncidentOutput.model_validate({"found": True})


def test_not_found_has_no_incident_fields() -> None:
    assert FindActiveIncidentOutput.model_validate({"found": False}).context_id is None
    with pytest.raises(ValidationError):
        FindActiveIncidentOutput.model_validate({"found": False, "context_id": "ctx_inc_x"})
    with pytest.raises(ValidationError):
        FindActiveIncidentOutput.model_validate({"found": True, "context_id": "c", "status": "x"})
