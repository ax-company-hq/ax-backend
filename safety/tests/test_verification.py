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


def _rollback(act, key: str = "ctx_inc_0001:rollback_deployment:dep_42") -> Any:
    return act(
        "rollback_deployment",
        "infra",
        key,
        deployment_id="dep_42",
        target_version="v1.4.2",
        summary="결제 오류 급증, 직전 배포 롤백",
    )


@pytest.mark.asyncio
async def test_rollback_rejection_goes_to_human_hold(gate: Gate, sent: Any, act) -> None:
    """롤백(on_verify_reject=hold): planning이 거절해도 끝내지 않고 사람 보류 큐(C)로 간다."""
    from ax_safety.gate import VerifyOutcome

    async def reject(task: Any) -> VerifyOutcome:
        assert (task.verifier, task.skill) == ("planning", "verify_rollback_target")
        return VerifyOutcome("rejected", {"status": "completed", "reason_code": "target_unsafe"})

    gate._verifier = reject
    decision = await gate.execute(_rollback(act))
    assert (decision.grade, decision.status) == ("C", "held")
    assert decision.hold_reason == HoldReason.VERIFY_REJECTED
    assert decision.verifier == "planning"
    reviews = gate.store.list_reviews("pending")
    assert [r.review_id for r in reviews] == [decision.review_id]
    report = reviews[0].report
    assert report["hold_reason"] == "verify_rejected"
    assert report["verification"]["verifier"] == "planning"
    assert report["verification"]["result"] == "rejected"
    assert report["verification"]["detail"]["reason_code"] == "target_unsafe"
    assert report["summary"] == "결제 오류 급증, 직전 배포 롤백"
    event_type, data = sent.sent[-1]
    assert event_type == "safety.action_held"
    assert (data["hold_reason"], data["review_id"]) == (
        HoldReason.VERIFY_REJECTED,
        decision.review_id,
    )
    audit = [e for e in gate.store.list_audit() if e.execution_id == decision.execution_id]
    steps = [e.step for e in audit]
    assert "verify_result" in steps and "decided" in steps
    decided = next(e for e in audit if e.step == "decided")
    assert (decided.detail["status"], decided.detail["hold_reason"]) == ("held", "verify_rejected")


@pytest.mark.asyncio
async def test_rollback_rejection_hold_approval_executes_same_key(
    gate: Gate, verifier: Any, act
) -> None:
    """보류된 롤백을 사람이 승인하면 같은 멱등성 키 재요청이 `executed`가 된다."""
    verifier.result = "rejected"
    held = await gate.execute(_rollback(act, key="rb-approve"))
    assert held.status == "held"
    await gate.decide_review(held.review_id or "", "approved", "예진", "대상 확인함")
    replay = await gate.execute(_rollback(act, key="rb-approve"))
    assert (replay.status, replay.grade) == ("executed", "C")
    assert len(verifier.tasks) == 1  # 재요청은 검증자를 다시 부르지 않는다


@pytest.mark.parametrize("result", ["unavailable", "raise"])
@pytest.mark.asyncio
async def test_rollback_verifier_silence_still_verify_timeout(
    gate: Gate, verifier: Any, act, result: str
) -> None:
    verifier.result = result
    decision = await gate.execute(_rollback(act, key=f"rb-silent-{result}"))
    assert (decision.status, decision.hold_reason) == ("held", HoldReason.VERIFY_TIMEOUT)


@pytest.mark.asyncio
async def test_rollback_verifier_approval_executes(gate: Gate, verifier: Any, act) -> None:
    decision = await gate.execute(_rollback(act, key="rb-ok"))
    assert (decision.grade, decision.status) == ("B", "executed")
    assert gate.store.list_reviews() == []


@pytest.mark.asyncio
async def test_other_agent_verified_rejection_stays_rejected(
    gate: Gate, verifier: Any, sent: Any, act
) -> None:
    """on_verify_reject가 없는 액션(restart_service)은 지금처럼 거절 = rejected, 보류 없음."""
    verifier.result = "rejected"
    decision = await gate.execute(act("restart_service", "infra", "rs-1", service="shop-api"))
    assert (decision.status, decision.hold_reason) == ("rejected", HoldReason.VERIFY_REJECTED)
    assert gate.store.list_reviews() == []
    assert sent.types() == []
