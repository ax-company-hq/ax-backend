"""hr 액션 등록 (#180): A 5개, B 2개, C 2개."""

from __future__ import annotations

from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate

A_ACTIONS = [
    "start_handover",
    "answer_handover_question",
    "request_assignment_change",
    "start_onboarding",
    "update_onboarding_step",
    "decide_candidate_card",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", A_ACTIONS)
async def test_hr_record_actions_are_a(gate: Gate, act, action: str) -> None:
    decision = await gate.execute(act(action, "hr", f"hr-{action}"))
    assert (decision.grade, decision.status) == ("A", "executed")
    other = await gate.execute(act(action, "support", f"hr-{action}-x"))
    assert other.hold_reason == HoldReason.UNREGISTERED_ACTION


@pytest.mark.asyncio
async def test_confirm_handover_is_verified_by_planning(gate: Gate, verifier: Any, act) -> None:
    ok = await gate.execute(act("confirm_handover", "hr", "ho-1", completeness_percent=80))
    assert (ok.grade, ok.status, ok.verifier) == ("B", "executed", "planning")
    assert verifier.tasks[-1].skill == "verify_handover"

    verifier.result = "rejected"
    rejected = await gate.execute(act("confirm_handover", "hr", "ho-2", completeness_percent=90))
    assert (rejected.status, rejected.hold_reason) == ("rejected", HoldReason.VERIFY_REJECTED)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"completeness_percent": 79}, {}])
async def test_confirm_handover_below_threshold_or_missing_is_held(
    gate: Gate, verifier: Any, act, params: dict[str, int]
) -> None:
    decision = await gate.execute(act("confirm_handover", "hr", f"ho-{len(params)}", **params))
    assert (decision.grade, decision.status) == ("C", "held")
    assert verifier.tasks == []  # C는 검증자에게 보내지 않는다


@pytest.mark.asyncio
async def test_confirm_reassignment_rules(gate: Gate, verifier: Any, act) -> None:
    ok = await gate.execute(
        act("confirm_reassignment", "hr", "re-ok", unassigned_count=0, duplicate_count=0)
    )
    assert (ok.grade, ok.status, ok.verifier) == ("B", "executed", "safety")
    assert verifier.tasks == []

    for key, bad in {
        "reassignment_gap": {"unassigned_count": 1, "duplicate_count": 0},
        "reassignment_duplicate": {"unassigned_count": 0, "duplicate_count": 2},
    }.items():
        decision = await gate.execute(act("confirm_reassignment", "hr", f"re-{key}", **bad))
        assert decision.status == "held"
        review = gate.store.get_review(decision.review_id or "")
        assert [hit["key"] for hit in review.report["rules"]] == [key]  # type: ignore[union-attr]

    missing = await gate.execute(act("confirm_reassignment", "hr", "re-none"))
    assert missing.status == "held"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm_handover_incomplete", "confirm_offboarding"])
async def test_hr_c_actions_always_held_with_summary(gate: Gate, act, action: str) -> None:
    decision = await gate.execute(
        act(action, "hr", f"c-{action}", summary="퇴직 확정 요청: emp_06 (support-desk)")
    )
    assert (decision.grade, decision.status) == ("C", "held")
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None
    assert review.report["summary"] == "퇴직 확정 요청: emp_06 (support-desk)"
    assert review.report["rules"] == [{"key": "base_grade", "value": "C"}]
