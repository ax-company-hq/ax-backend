"""memory Agent Card 검증."""

from fastapi.testclient import TestClient

from ax_agent_template.config import DEFAULT_PORTS
from ax_memory import handlers
from ax_memory.main import app, load_card

REQUESTERS = {
    "orchestrator",
    "planning",
    "support",
    "marketing",
    "hr",
    "finance",
    "data",
    "infra",
    "safety",
}


def _skill(card, skill_id):
    return next(s for s in card.skills if s.id == skill_id)


def test_카드가_AgentCard로_검증되고_핸들러와_일치한다() -> None:
    card = load_card()
    assert card.name == "memory"
    assert card.url == "http://memory:8002/a2a"
    assert {s.id for s in card.skills} == set(handlers.HANDLERS)
    assert TestClient(app).get("/.well-known/agent-card.json").json()["name"] == "memory"


def test_상태를_바꾸는_skill은_A등급이다() -> None:
    card = load_card()
    assert _skill(card, "card_write").grade == "A"
    assert _skill(card, "card_link").grade == "A"
    assert _skill(card, "card_search").grade == "read"
    assert _skill(card, "card_get").grade == "read"


def test_card_link는_hr만_요청할_수_있다() -> None:
    assert _skill(load_card(), "card_link").allowed_requesters == ["hr"]


def test_요청자_목록은_실제_에이전트_이름이다() -> None:
    card = load_card()
    known = set(DEFAULT_PORTS)
    for skill in card.skills:
        for name in skill.allowed_requesters:
            assert name in known or name == "dashboard-api"
    assert set(_skill(card, "card_search").allowed_requesters) == REQUESTERS
    assert set(_skill(card, "card_get").allowed_requesters) == REQUESTERS | {"dashboard-api"}
    assert "marketing" in _skill(card, "card_write").allowed_requesters
    assert all(s.verifier is None for s in card.skills)
