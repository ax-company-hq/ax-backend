import json
from pathlib import Path

import pytest

from ax_orchestrator.mock.harness import DEPTS
from ax_orchestrator.registry import Registry

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_skill_to_dept_map(harness) -> None:
    await harness.registry.load(DEPTS, harness.fakes.fetch_card)
    assert harness.registry.find("impact_analysis").dept == "data"
    assert harness.registry.find("issue_coupon").verifier == "finance"
    assert harness.registry.find("nope") is None


@pytest.mark.asyncio
async def test_unreachable_or_empty_card_is_marked_unavailable(harness) -> None:
    harness.fakes.down.add("data")
    harness.fakes.empty_cards.add("infra")
    await harness.registry.load(DEPTS, harness.fakes.fetch_card)
    assert "data" in harness.registry.unavailable
    assert "infra" in harness.registry.unavailable
    assert harness.registry.find("impact_analysis") is None
    assert harness.registry.find("record_case") is not None


@pytest.mark.asyncio
async def test_fetch_exception_is_contained() -> None:
    async def boom(dept: str):
        raise ConnectionError(dept)

    reg = Registry()
    await reg.load(["data"], boom)
    assert reg.unavailable["data"].startswith("수집 실패")


@pytest.mark.asyncio
async def test_duplicate_skill_is_recorded_as_conflict(harness) -> None:
    async def fetch(dept: str):
        card = await harness.fakes.fetch_card(dept)
        if dept == "support" and card:
            card["skills"].append({**card["skills"][0], "id": "impact_analysis"})
        return card

    reg = Registry()
    await reg.load(DEPTS, fetch)
    assert reg.find("impact_analysis").dept == "data"
    assert reg.conflicts


@pytest.mark.asyncio
async def test_real_dev_cards_load() -> None:
    names = {
        "data": "data",
        "finance": "finance",
        "marketing": "marketing",
        "planning": "planning",
        "hr": "hr",
        "infra": "infra-dejafix",
    }
    cards = {
        dept: json.loads((ROOT / "agents" / folder / "agent-card.json").read_text(encoding="utf-8"))
        for dept, folder in names.items()
    }

    async def fetch(dept: str):
        return cards[dept]

    reg = Registry()
    await reg.load(list(cards), fetch)
    assert reg.find("impact_analysis").dept == "data"
    assert reg.find("record_case").dept == "hr"
    assert "infra" in reg.unavailable  # infra 카드가 비어 있는 현재 상태
