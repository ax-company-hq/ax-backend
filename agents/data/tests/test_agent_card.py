"""data Agent Card 검증 (이슈 #42)."""

import json
from pathlib import Path

import pytest

from ax_contracts.schemas.agent_card import AgentCard

CARD_PATH = Path(__file__).resolve().parents[1] / "agent-card.json"


@pytest.fixture(scope="module")
def card() -> AgentCard:
    return AgentCard.model_validate(json.loads(CARD_PATH.read_text(encoding="utf-8")))


def test_card_validates(card: AgentCard) -> None:
    assert card.name == "data"
    assert card.url == "http://data:8106/a2a"
    assert {s.id for s in card.skills} == {
        "anomaly_detection",
        "impact_analysis",
        "fraud_score",
        "kpi_summary",
    }


def test_all_skills_are_read(card: AgentCard) -> None:
    assert all(s.grade == "read" for s in card.skills)


def test_fraud_score_requesters(card: AgentCard) -> None:
    skill = next(s for s in card.skills if s.id == "fraud_score")
    assert set(skill.allowed_requesters) == {"orchestrator", "finance"}
    assert len(skill.allowed_requesters) == 2


def test_impact_analysis_requesters(card: AgentCard) -> None:
    skill = next(s for s in card.skills if s.id == "impact_analysis")
    assert set(skill.allowed_requesters) == {
        "orchestrator",
        "infra",
        "support",
        "finance",
        "planning",
    }
    assert skill.priority == "required"
