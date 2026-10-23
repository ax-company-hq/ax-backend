"""B 교차검증 테스트 (재무 구성안 8장 단독 테스트 ⑤, SAF-08): 검증자 무응답·지연·거절."""

from __future__ import annotations

from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate


@pytest.mark.parametrize("result", ["unavailable", "raise"])
@pytest.mark.asyncio
async def test_verifier_silence_never_executes(
    gate: Gate, verifier: Any, result: str, coupon
) -> None:
    """⑤ 검증자 무응답·오류 → 실행하지 않고 C(`verify_timeout`), fail-closed (SAF-08)."""
    verifier.result = result
    decision = await gate.execute(coupon())
    assert (decision.grade, decision.status) == ("C", "held")
    assert decision.hold_reason == HoldReason.VERIFY_TIMEOUT
    assert gate.store.executed_total("shop-demo", "issue_coupon") == 0


@pytest.mark.asyncio
async def test_verifier_rejection(gate: Gate, verifier: Any, sent: Any, coupon) -> None:
    verifier.result = "rejected"
    decision = await gate.execute(coupon())
    assert (decision.status, decision.hold_reason) == ("rejected", HoldReason.VERIFY_REJECTED)
    assert gate.store.list_reviews() == []  # 재무가 거절한 건은 보류 큐에 넣지 않는다
    assert sent.types() == []


@pytest.mark.asyncio
async def test_verify_task_carries_code_amount_not_claims(
    gate: Gate, verifier: Any, coupon
) -> None:
    await gate.execute(coupon(3, 5000, claimed_grade="A"))
    task = verifier.tasks[0]
    assert (task.verifier, task.skill) == ("finance", "verify_amount")
    assert task.input["computed_amount"] == 15_000
    assert task.input["threshold_version"] == 1
    assert task.timeout_seconds == 30


@pytest.mark.asyncio
async def test_slow_verifier_times_out(gate: Gate, monkeypatch: pytest.MonkeyPatch, coupon) -> None:
    """검증자가 마감(기준표 verify_timeout_seconds)보다 늦으면 실행하지 않고 보류한다."""
    import asyncio

    async def slow(task: Any) -> Any:
        await asyncio.sleep(1)

    gate._verifier = slow
    gate.thresholds = gate.thresholds.model_copy(update={"verify_timeout_seconds": 0.05})
    decision = await gate.execute(coupon(key="slow-verify"))
    assert (decision.status, decision.hold_reason) == ("held", HoldReason.VERIFY_TIMEOUT)
    assert gate.store.executed_total("shop-demo", "issue_coupon") == 0
