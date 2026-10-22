"""AI 1차 판단 테스트: 심각하지 않은 C 건은 사람보다 먼저 AI·규칙이 결정한다."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ax_contracts.schemas import AgentResult, HoldReason, RequestMeta, TaskStatus
from ax_safety import verify
from ax_safety.gate import AI_REVIEWER, RULE_REVIEWER, Gate, ReviewOutcome, ReviewTask
from ax_safety.rules import load_registry, load_thresholds
from ax_safety.store import Store


class FakeReviewer:
    def __init__(self, outcome: ReviewOutcome | Exception) -> None:
        self.outcome = outcome
        self.tasks: list[ReviewTask] = []

    async def __call__(self, task: ReviewTask) -> ReviewOutcome:
        self.tasks.append(task)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def make_gate(clock: Any, verifier: Any, sent: Any, reviewer: Any, enabled: bool = True) -> Gate:
    table = load_thresholds()
    table = table.model_copy(
        update={"auto_review": table.auto_review.model_copy(update={"enabled": enabled})}
    )
    return Gate(load_registry(), table, Store(), verifier, sent, now=clock, reviewer=reviewer)


APPROVE = ReviewOutcome("approve", "within_ai_limit", "재량 한도 안이라 승인")
BIG = {"count": 20, "unit_value": 20_000}  # 400,000원: 쿠폰 건별 30만 기준 초과


def test_config_is_on_and_lists_finance_actions() -> None:
    cfg = load_thresholds().auto_review
    assert cfg.enabled and (cfg.reviewer, cfg.skill) == ("finance", "review_hold")
    assert set(cfg.actions) == {"issue_coupon", "refund_request", "seller_review"}
    assert "confirm_settlement" not in cfg.actions  # 정산 확정은 항상 사람


@pytest.mark.asyncio
async def test_ai_approval_executes_without_human(clock, verifier, sent, coupon) -> None:
    reviewer = FakeReviewer(APPROVE)
    gate = make_gate(clock, verifier, sent, reviewer)
    decision = await gate.execute(coupon(**BIG, purpose_code="campaign", key="ai-ok"))
    assert (decision.grade, decision.status) == ("C", "executed")
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None
    assert (review.status, review.decided_by, review.note) == (
        "approved",
        AI_REVIEWER,
        "재량 한도 안이라 승인",
    )
    assert gate.store.list_reviews("pending") == []
    assert sent.types()[-1] == "safety.review_decided"
    # AI가 승인한 금액도 누적 한도에 들어간다
    assert gate.store.executed_total("shop-demo", "issue_coupon") == 400_000


@pytest.mark.asyncio
async def test_ai_reject(clock, verifier, sent, coupon) -> None:
    gate = make_gate(
        clock,
        verifier,
        sent,
        FakeReviewer(ReviewOutcome("reject", "not_affected", "피해 고객 아님")),
    )
    decision = await gate.execute(coupon(**BIG, key="ai-no"))
    assert decision.status == "rejected"
    assert gate.store.get_review(decision.review_id or "").decided_by == AI_REVIEWER  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "outcome", [ReviewOutcome("escalate", "over_ai_limit", "큰 금액"), ConnectionError("down")]
)
@pytest.mark.asyncio
async def test_escalate_or_silence_leaves_it_to_humans(
    clock, verifier, sent, coupon, outcome
) -> None:
    gate = make_gate(clock, verifier, sent, FakeReviewer(outcome))
    decision = await gate.execute(coupon(**BIG, purpose_code="campaign", key="ai-up"))
    assert (decision.status, decision.hold_reason) == ("held", HoldReason.OVER_THRESHOLD)
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None and review.status == "pending"
    assert review.report["ai_review"]["result"] == "escalate"


@pytest.mark.asyncio
async def test_slow_ai_leaves_it_to_humans(clock, verifier, sent, coupon) -> None:
    async def slow(task: ReviewTask) -> ReviewOutcome:
        await asyncio.sleep(1)
        return APPROVE

    gate = make_gate(clock, verifier, sent, slow)
    gate.thresholds = gate.thresholds.model_copy(
        update={
            "auto_review": gate.thresholds.auto_review.model_copy(update={"timeout_seconds": 0.05})
        }
    )
    decision = await gate.execute(coupon(**BIG, purpose_code="campaign", key="ai-slow"))
    assert decision.status == "held"


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (
            lambda c, a: c(3, 5000, key="mm").model_copy(
                update={"params": {**c(3, 5000).params, "total_value": 1}}
            ),
            HoldReason.AMOUNT_MISMATCH,
        ),
        (lambda c, a: a("delete_orders", "marketing", "del"), HoldReason.UNREGISTERED_ACTION),
        (
            lambda c, a: c(key="bad").model_copy(update={"params": {**c().params, "count": "2"}}),
            HoldReason.SCHEMA_INVALID,
        ),
    ],
)
@pytest.mark.asyncio
async def test_bad_requests_are_rejected_by_rule(
    clock, verifier, sent, coupon, act, make, reason
) -> None:
    """요청 자체가 잘못된 건은 AI에게 묻지도 않고 규칙이 바로 거절한다."""
    reviewer = FakeReviewer(APPROVE)
    gate = make_gate(clock, verifier, sent, reviewer)
    decision = await gate.execute(make(coupon, act))
    assert (decision.status, decision.hold_reason) == ("rejected", reason)
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None and review.decided_by == RULE_REVIEWER and review.note
    assert reviewer.tasks == []


@pytest.mark.asyncio
async def test_settlement_confirmation_still_needs_a_human(clock, verifier, sent, act) -> None:
    reviewer = FakeReviewer(APPROVE)
    gate = make_gate(clock, verifier, sent, reviewer)
    decision = await gate.execute(act("confirm_settlement", "finance", "stl", seller_id="sel_1"))
    assert decision.status == "held"
    assert reviewer.tasks == []


@pytest.mark.asyncio
async def test_disabled_means_humans_decide_everything(clock, verifier, sent, coupon) -> None:
    reviewer = FakeReviewer(APPROVE)
    gate = make_gate(clock, verifier, sent, reviewer, enabled=False)
    mismatch = coupon(3, 5000, key="off-mm")
    mismatch.params["total_value"] = 1
    assert (await gate.execute(mismatch)).status == "held"
    assert (await gate.execute(coupon(**BIG, key="off-big"))).status == "held"
    assert reviewer.tasks == []


@pytest.mark.asyncio
async def test_reviewer_gets_amounts_rules_and_totals(clock, verifier, sent, coupon) -> None:
    reviewer = FakeReviewer(ReviewOutcome("escalate"))
    gate = make_gate(clock, verifier, sent, reviewer)
    await gate.execute(coupon(3, 20_000, key="facts"))  # 고객당 보상 6만 원
    task = reviewer.tasks[0]
    assert (task.reviewer, task.skill) == ("finance", "review_hold")
    data = task.input
    assert (data["action"], data["computed_amount"], data["hold_reason"]) == (
        "issue_coupon",
        60_000,
        "over_threshold",
    )
    assert data["rules"][0]["key"] == "compensation_per_customer"
    assert data["totals"] == {
        "daily_total": 60_000,
        "context_total": 60_000,
        "customer_total": 60_000,
    }


@pytest.mark.asyncio
async def test_call_reviewer_maps_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    def reply(status: TaskStatus, output: dict) -> Any:
        async def fake(target: str, req: Any) -> AgentResult:
            return AgentResult(
                meta=RequestMeta.model_validate(req.meta.model_dump()), status=status, output=output
            )

        return fake

    task = ReviewTask("finance", "review_hold", "ctx_inc_0001", "shop-demo", "rev_1", 10, {})
    cases = [
        (TaskStatus.COMPLETED, {"decision": "approve", "explanation": "ok"}, "approve"),
        (TaskStatus.COMPLETED, {"decision": "reject"}, "reject"),
        (TaskStatus.COMPLETED, {"decision": "escalate"}, "escalate"),
        (TaskStatus.COMPLETED, {"decision": "yes"}, "escalate"),
        (TaskStatus.FAILED, {}, "escalate"),
        (TaskStatus.CANCELED, {}, "escalate"),
    ]
    for status, output, expected in cases:
        monkeypatch.setattr(verify, "call_agent", reply(status, output))
        assert (await verify.call_reviewer(task)).result == expected
