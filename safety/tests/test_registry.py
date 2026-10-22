import pytest

from ax_contracts.schemas.execute import HoldReason
from ax_safety.registry import GradeTable

WRITERS = ["hr", "support", "marketing", "planning", "finance", "orchestrator", "data"]


@pytest.mark.parametrize("requester", WRITERS)
def test_card_write_is_grade_a(requester: str) -> None:
    verdict = GradeTable.load().judge("memory.card_write", requester)
    assert verdict.allowed
    assert verdict.grade == "A"


def test_card_link_only_hr() -> None:
    table = GradeTable.load()
    assert table.judge("memory.card_link", "hr").grade == "A"
    assert table.judge("memory.card_link", "hr").allowed
    assert not table.judge("memory.card_link", "support").allowed


@pytest.mark.parametrize("action", ["memory.card_write", "memory.card_link"])
def test_unlisted_requester_rejected(action: str) -> None:
    verdict = GradeTable.load().judge(action, "unknown")
    assert not verdict.allowed


def test_unregistered_action_held() -> None:
    verdict = GradeTable.load().judge("memory.card_delete", "hr")
    assert not verdict.allowed
    assert verdict.grade == "C"
    assert verdict.hold_reason == HoldReason.UNREGISTERED_ACTION
