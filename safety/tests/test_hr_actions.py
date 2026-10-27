"""hr 액션 등록 (#180, #375): A 3개, B 2개, C 1개. 온보딩·발령 요청·퇴직 확정은 hr에서 빠졌다."""

from __future__ import annotations

from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate

A_ACTIONS = [
    "start_handover",
    "answer_handover_question",
    "decide_candidate_card",
]

# hr에서 뺀 액션 (#374, #375). 등록하지 않으니 무엇을 보내도 미등록이라 C(사람 승인)다.
REMOVED_ACTIONS = [
    "confirm_reassignment",  # thresholds.toml의 reassignment_* 규칙과 함께 뺐다(#375)
    "request_assignment_change",
    "start_onboarding",
    "update_onboarding_step",
    "confirm_offboarding",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", REMOVED_ACTIONS)
async def test_removed_hr_actions_are_unregistered_and_held(gate: Gate, act, action: str) -> None:
    decision = await gate.execute(act(action, "hr", f"removed-{action}"))
    assert (decision.grade, decision.status) == ("C", "held")
    assert decision.hold_reason == HoldReason.UNREGISTERED_ACTION


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
@pytest.mark.parametrize("action", ["confirm_handover_incomplete"])
async def test_hr_c_actions_always_held_with_summary(gate: Gate, act, action: str) -> None:
    summary = "인수인계 미완성 확정 요청: emp_06 (support-desk)"
    decision = await gate.execute(act(action, "hr", f"c-{action}", summary=summary))
    assert (decision.grade, decision.status) == ("C", "held")
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None
    assert review.report["summary"] == summary
    assert review.report["rules"] == [{"key": "base_grade", "value": "C"}]
