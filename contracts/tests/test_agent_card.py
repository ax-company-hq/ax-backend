import json

import pytest
from pydantic import ValidationError

from ax_contracts.schemas import AgentCard

HR_CARD_JSON = json.dumps(
    {
        "name": "hr",
        "owner": "준하",
        "description": "사건·결정 기록",
        "url": "http://hr:8104/a2a",
        "version": "0.1.0",
        "capabilities": {"streaming": True, "push_notifications": False},
        "skills": [
            {
                "id": "record_case",
                "description": "사건 기록을 받아 결정 카드를 만든다.",
                "input_schema": "contracts/schemas/record.py#RecordCase",
                "output_schema": "contracts/schemas/record.py#RecordCase",
                "grade": "A",
                "allowed_requesters": ["orchestrator", "planning", "support", "infra"],
                "verifier": None,
                "priority": "required",
            }
        ],
    }
)


def _card(**skill_overrides) -> dict:
    card = json.loads(HR_CARD_JSON)
    card["skills"][0].update(skill_overrides)
    return card


def test_example_json_validates() -> None:
    card = AgentCard.model_validate_json(HR_CARD_JSON)
    assert card.name == "hr"
    assert card.skills[0].grade == "A"


def test_b_grade_requires_verifier() -> None:
    with pytest.raises(ValidationError, match="verifier"):
        AgentCard.model_validate(_card(grade="B", verifier=None))


def test_b_grade_with_other_verifier_is_ok() -> None:
    card = AgentCard.model_validate(_card(grade="B", verifier="finance"))
    assert card.skills[0].verifier == "finance"


def test_self_verification_is_forbidden() -> None:
    with pytest.raises(ValidationError, match="자기 검증 금지"):
        AgentCard.model_validate(_card(grade="B", verifier="hr"))


def test_camel_case_field_is_rejected() -> None:
    data = _card()
    data["skills"][0]["allowedRequesters"] = ["orchestrator"]
    with pytest.raises(ValidationError):
        AgentCard.model_validate(data)


def test_invalid_grade_and_priority_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentCard.model_validate(_card(grade="D"))
    with pytest.raises(ValidationError):
        AgentCard.model_validate(_card(priority="must"))


def test_skill_id_must_be_snake_case() -> None:
    with pytest.raises(ValidationError):
        AgentCard.model_validate(_card(id="Record-Case"))


def test_duplicate_skill_ids_are_rejected() -> None:
    data = json.loads(HR_CARD_JSON)
    data["skills"].append(dict(data["skills"][0]))
    with pytest.raises(ValidationError, match="중복"):
        AgentCard.model_validate(data)
