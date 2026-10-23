"""실행 관문 판정 테스트 (재무 구성안 8장 단독 테스트 ①~⑥, SAF-01·03·04·05·08·10)."""

from __future__ import annotations

from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate
from ax_safety.rules import ActionSpec, ThresholdTable, load_registry, load_thresholds


def test_rule_files_load_and_match_agreed_values() -> None:
    registry, table = load_registry(), load_thresholds()
    assert table.version == 1
    assert table.review_timeout_seconds == 600  # 데모 10분 자동 거절
    values = {rule.key: rule.value for rule in table.rules if rule.metric != "param"}
    values.pop("pr_allowed_paths")
    values.pop("merge_allowed_paths")
    assert values == {
        "coupon_total": 300_000,
        "coupon_unit": 20_000,
        "coupon_daily": 1_000_000,
        "compensation_per_context": 1_000_000,
        "compensation_per_customer": 50_000,
        "refund_single": 100_000,
    }
    issue = registry.get("issue_coupon")
    assert issue is not None and (issue.base_grade, issue.verifier) == ("B", "finance")
    assert registry.get("confirm_settlement").base_grade == "C"  # type: ignore[union-attr]
    for spec in registry.actions:  # 자기 검증 금지
        assert spec.verifier != spec.owner


def test_duplicate_rule_keys_are_refused() -> None:
    rule = {"key": "dup", "action": "x", "metric": "amount", "value": 1}
    with pytest.raises(ValueError, match="같은 규칙 키가 두 번 있다: dup"):
        ThresholdTable.model_validate(
            {
                "version": 1,
                "verify_timeout_seconds": 30,
                "review_timeout_seconds": 600,
                "rules": [rule, {**rule, "action": "y"}],
            }
        )


def test_self_verification_is_refused_at_registration() -> None:
    with pytest.raises(ValueError, match="자기 검증"):
        ActionSpec(
            action="x", owner="finance", base_grade="B", verifier="finance", verify_skill="v"
        )
    with pytest.raises(ValueError, match="verifier"):
        ActionSpec(action="x", owner="marketing", base_grade="B")
    with pytest.raises(ValueError, match="자기 검증"):  # 요청자 목록에 검증자가 있어도 금지
        ActionSpec(
            action="x",
            owner="memory",
            requesters=["hr", "data"],
            base_grade="B",
            verifier="data",
            verify_skill="v",
        )


@pytest.mark.asyncio
async def test_grade_a_executes_immediately(gate: Gate, sent: Any, act) -> None:
    decision = await gate.execute(act("publish_notice", "marketing", "k1", draft_id="d1"))
    assert (decision.grade, decision.status) == ("A", "executed")
    assert decision.execution_id.startswith("exe_")
    assert sent.types() == ["safety.action_executed"]


@pytest.mark.asyncio
async def test_claimed_grade_is_ignored(gate: Gate, verifier: Any, coupon) -> None:
    """① 등급은 규칙이 정한다: `claimed_grade="A"`여도 50,000원은 B, 400,000원은 C."""
    small = await gate.execute(coupon(10, 5000, purpose_code="campaign", claimed_grade="A"))
    assert (small.grade, small.status, small.verifier) == ("B", "executed", "finance")
    assert small.computed_amount == 50_000
    assert len(verifier.tasks) == 1

    big = await gate.execute(coupon(20, 20_000, purpose_code="campaign", claimed_grade="A"))
    assert (big.grade, big.status, big.hold_reason) == ("C", "held", HoldReason.OVER_THRESHOLD)
    assert big.review_id and big.review_id.startswith("rev_")
    assert len(verifier.tasks) == 1  # C는 검증자에게 보내지 않는다


@pytest.mark.asyncio
async def test_amount_mismatch_holds(gate: Gate, verifier: Any, coupon) -> None:
    """② 요청 총액 10,000원 ≠ 코드 계산 50,000원 → C(`amount_mismatch`)."""
    req = coupon(10, 5000, purpose_code="campaign")
    req.params["total_value"] = 10_000
    decision = await gate.execute(req)
    assert (decision.status, decision.hold_reason) == ("held", HoldReason.AMOUNT_MISMATCH)
    assert decision.computed_amount == 50_000
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None
    assert review.report["calculation"] == "10 × 5,000 = 50,000원"
    assert review.report["claimed"] == {"total_value": 10_000}
    assert verifier.tasks == []


@pytest.mark.asyncio
async def test_daily_split_requests_are_caught(gate: Gate, coupon) -> None:
    """③ 250,000원 × 5회: 4회째에 일일 누적 1,000,000원 도달 → C(`coupon_daily`)."""
    statuses = []
    for i in range(5):
        req = coupon(25, 10_000, purpose_code="campaign", key=f"split-{i}")
        statuses.append((await gate.execute(req)).status)
    assert statuses == ["executed", "executed", "executed", "held", "held"]
    held = gate.store.list_reviews("pending")[0]
    assert held.report["rules"][0]["key"] == "coupon_daily"
    assert held.report["rules"][0]["observed"] == 1_000_000


@pytest.mark.asyncio
async def test_daily_total_resets_next_day(gate: Gate, clock: Any, coupon) -> None:
    for i in range(3):
        req = coupon(25, 10_000, purpose_code="campaign", key=f"d1-{i}")
        assert (await gate.execute(req)).status == "executed"
    clock.advance(24 * 3600)
    assert (await gate.execute(coupon(25, 10_000, purpose_code="campaign", key="d2"))).status == (
        "executed"
    )


@pytest.mark.asyncio
async def test_compensation_per_customer_limit(gate: Gate, coupon) -> None:
    """같은 사건·같은 고객 보상 누적 5만 원 이상이면 C."""
    first = await gate.execute(coupon(4, 10_000, key="c1"))
    assert first.status == "executed"
    second = await gate.execute(coupon(1, 10_000, key="c2"))
    assert (second.status, second.hold_reason) == ("held", HoldReason.OVER_THRESHOLD)
    other_buyer = await gate.execute(coupon(1, 10_000, key="c3", buyer_id="buy_b2"))
    assert other_buyer.status == "executed"


@pytest.mark.asyncio
async def test_unit_value_limit(gate: Gate, coupon) -> None:
    decision = await gate.execute(coupon(1, 20_001, purpose_code="campaign"))
    assert decision.status == "held"
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None and review.report["rules"][0]["key"] == "coupon_unit"


@pytest.mark.asyncio
async def test_idempotent_replay(gate: Gate, verifier: Any, sent: Any, coupon) -> None:
    """④ 같은 멱등성 키 두 번 → 판정·검증·이벤트는 한 번, 두 번째는 이전 결과."""
    first = await gate.execute(coupon(key="same"))
    second = await gate.execute(coupon(key="same"))
    assert first == second
    assert len(verifier.tasks) == 1
    assert sent.types() == ["safety.action_executed"]
    assert [e.step for e in gate.store.list_audit()][-1] == "idempotent_replay"


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
async def test_unregistered_action_holds(gate: Gate, sent: Any, act) -> None:
    """⑥ 미등록 액션 → C(`unregistered_action`), 아무것도 실행하지 않는다."""
    decision = await gate.execute(act("delete_orders", "marketing", "k-del"))
    assert (decision.status, decision.hold_reason) == ("held", HoldReason.UNREGISTERED_ACTION)
    assert sent.types() == ["safety.action_held"]


@pytest.mark.asyncio
async def test_action_of_another_department_is_unregistered(gate: Gate, coupon) -> None:
    """support가 marketing의 쿠폰 액션을 직접 요청하면 신고하지 않은 액션이다."""
    req = coupon().model_copy(update={"requester": "support"})
    decision = await gate.execute(req)
    assert decision.hold_reason == HoldReason.UNREGISTERED_ACTION


@pytest.mark.parametrize("bad", ["10", 10.0, -1, None, True])
@pytest.mark.asyncio
async def test_invalid_amount_fields_hold(gate: Gate, bad: object, coupon) -> None:
    req = coupon(purpose_code="campaign")
    req.params["count"] = bad
    decision = await gate.execute(req)
    assert (decision.status, decision.hold_reason) == ("held", HoldReason.SCHEMA_INVALID)


@pytest.mark.asyncio
async def test_settlement_confirmation_is_always_held(gate: Gate, act) -> None:
    decision = await gate.execute(act("confirm_settlement", "finance", "k-set", draft="s1"))
    assert (decision.grade, decision.status) == ("C", "held")


@pytest.mark.asyncio
async def test_refund_threshold(gate: Gate, verifier: Any, act) -> None:
    small = await gate.execute(act("refund_request", "support", "r1", refund_amount=99_999))
    assert (small.grade, small.status) == ("B", "executed")
    big = await gate.execute(act("refund_request", "support", "r2", refund_amount=100_000))
    assert big.status == "held"


@pytest.mark.asyncio
async def test_review_approval_turns_replay_into_executed(gate: Gate, sent: Any, coupon) -> None:
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="big"))
    review = await gate.decide_review(held.review_id or "", "approved", "태윤", "확인함")
    assert (review.status, review.decided_by) == ("approved", "태윤")
    replay = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="big"))
    assert (replay.status, replay.grade) == ("executed", "C")
    assert sent.types()[-1] == "safety.review_decided"
    with pytest.raises(ValueError):
        await gate.decide_review(held.review_id or "", "rejected", "태윤", None)


@pytest.mark.asyncio
async def test_review_auto_rejects_after_timeout(gate: Gate, clock: Any, sent: Any, coupon) -> None:
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="slow"))
    clock.advance(599)
    assert await gate.expire_reviews() == []
    clock.advance(1)
    expired = await gate.expire_reviews()
    assert [r.review_id for r in expired] == [held.review_id]
    replay = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="slow"))
    assert (replay.status, replay.hold_reason) == ("rejected", HoldReason.REVIEW_TIMEOUT)
    event_type, data = sent.sent[-1]
    assert (event_type, data["decision"], data["decided_by"]) == (
        "safety.review_decided",
        "rejected",
        "system",
    )


@pytest.mark.asyncio
async def test_every_decision_is_audited_with_context(gate: Gate, coupon) -> None:
    """SAF-06: 접수·판정이 context_id와 함께 남는다."""
    await gate.execute(coupon())
    steps = [(e.step, e.context_id) for e in gate.store.list_audit("ctx_inc_0001")]
    assert [s for s, _ in steps] == ["received", "verify_requested", "verify_result", "decided"]


def test_threshold_rules_must_reference_registered_actions(verifier: Any, sent: Any) -> None:
    table = load_thresholds()
    bad = table.model_copy(
        update={"rules": [*table.rules, table.rules[0].model_copy(update={"action": "ghost"})]}
    )
    from ax_safety.store import Store

    with pytest.raises(ValueError, match="ghost"):
        Gate(load_registry(), bad, Store(), verifier, sent)


WRITERS = ["hr", "support", "marketing", "planning", "finance", "orchestrator", "data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", WRITERS)
async def test_memory_card_write_is_a_for_listed_writers(gate: Gate, act, requester: str) -> None:
    """#41: `memory.card_write`는 A, 요청 가능 부서는 7곳."""
    decision = await gate.execute(act("memory.card_write", requester, f"card-{requester}"))
    assert (decision.grade, decision.status) == ("A", "executed")


@pytest.mark.asyncio
async def test_memory_card_link_only_hr(gate: Gate, act) -> None:
    ok = await gate.execute(act("memory.card_link", "hr", "link-1"))
    assert (ok.grade, ok.status) == ("A", "executed")
    other = await gate.execute(act("memory.card_link", "support", "link-2"))
    assert (other.status, other.hold_reason) == ("held", HoldReason.UNREGISTERED_ACTION)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["memory.card_write", "memory.card_link"])
async def test_memory_actions_reject_unlisted_requester(gate: Gate, act, action: str) -> None:
    decision = await gate.execute(act(action, "shop-api", f"x-{action}"))
    assert decision.status == "held"
    assert decision.hold_reason == HoldReason.UNREGISTERED_ACTION


@pytest.mark.asyncio
async def test_generate_pr_is_verified_by_code_rules(gate: Gate, verifier: Any, act) -> None:
    """planning `generate_pr`: 검증자는 에이전트가 아니라 변경 크기·허용 경로 규칙."""
    ok = await gate.execute(
        act(
            "generate_pr",
            "planning",
            "pr-ok",
            changed_lines=120,
            changed_files=4,
            changed_paths=["shop-api/app/payment.py", "docs/spec.md"],
        )
    )
    assert (ok.grade, ok.status, ok.verifier) == ("B", "executed", "safety")
    assert verifier.tasks == []  # 검증자 에이전트를 부르지 않는다

    too_big = await gate.execute(
        act(
            "generate_pr",
            "planning",
            "pr-big",
            changed_lines=301,
            changed_files=4,
            changed_paths=["docs/a.md"],
        )
    )
    assert too_big.status == "held"
    outside = await gate.execute(
        act(
            "generate_pr",
            "planning",
            "pr-out",
            changed_lines=10,
            changed_files=1,
            changed_paths=[".github/workflows/ci.yml"],
        )
    )
    review = gate.store.get_review(outside.review_id or "")
    assert review is not None
    assert review.report["rules"][0]["outside"] == [".github/workflows/ci.yml"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["create_ticket", "write_spec", "split_tasks", "review_pr", "send_message"]
)
async def test_planning_record_actions_are_a(gate: Gate, act, action: str) -> None:
    decision = await gate.execute(act(action, "planning", f"plan-{action}"))
    assert (decision.grade, decision.status) == ("A", "executed")


@pytest.mark.asyncio
async def test_confirm_spec_rules(gate: Gate, act) -> None:
    clean = dict(open_questions=0, warnings=0, basis_count=2, irreversible_count=0)
    ok = await gate.execute(act("confirm_spec", "planning", "spec-ok", **clean))
    assert (ok.grade, ok.status) == ("A", "executed")

    for key, bad in {
        "spec_open_question": {"open_questions": 1},
        "spec_rejected_warning": {"warnings": 1},
        "spec_no_basis": {"basis_count": 0},
        "irreversible": {"irreversible_count": 1},
    }.items():
        decision = await gate.execute(
            act("confirm_spec", "planning", f"spec-{key}", **{**clean, **bad})
        )
        assert decision.status == "held"
        review = gate.store.get_review(decision.review_id or "")
        assert [hit["key"] for hit in review.report["rules"]] == [key]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_merge_pr_rules(gate: Gate, verifier: Any, act) -> None:
    clean = dict(
        changed_lines=100,
        changed_files=3,
        changed_paths=["docs/a.md"],
        review_mismatch_count=1,
        linked_incident_count=0,
        irreversible_count=0,
    )
    ok = await gate.execute(act("merge_pr", "planning", "merge-ok", **clean))
    assert (ok.grade, ok.status, ok.verifier) == ("B", "executed", "safety")
    assert verifier.tasks == []

    for key, bad in {
        "merge_changed_lines": {"changed_lines": 301},
        "merge_changed_files": {"changed_files": 21},
        "merge_allowed_paths": {"changed_paths": [".github/x.yml"]},
        "pr_review_mismatch": {"review_mismatch_count": 2},
        "pr_linked_incident": {"linked_incident_count": 1},
        "merge_irreversible": {"irreversible_count": 1},
    }.items():
        decision = await gate.execute(
            act("merge_pr", "planning", f"merge-{key}", **{**clean, **bad})
        )
        assert decision.status == "held"
        review = gate.store.get_review(decision.review_id or "")
        assert [hit["key"] for hit in review.report["rules"]] == [key]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_missing_rule_fields_fail_closed(gate: Gate, act) -> None:
    """금액이 아닌 규칙의 필드가 없으면 통과시키지 않는다."""
    decision = await gate.execute(act("generate_pr", "planning", "pr-none", changed_lines=10))
    assert decision.status == "held"
    review = gate.store.get_review(decision.review_id or "")
    assert {hit["key"] for hit in review.report["rules"]} == {  # type: ignore[union-attr]
        "pr_changed_files",
        "pr_allowed_paths",
    }


@pytest.mark.asyncio
async def test_seller_review_risk_score(gate: Gate, verifier: Any, act) -> None:
    """판매자 승인(마켓운영): 위험 점수 70 이상이면 C, 미만이면 data가 교차검증(B)."""
    low = await gate.execute(
        act("seller_review", "finance", "sel-1", seller_id="sel_1", risk_score=20)
    )
    assert (low.grade, low.status, low.verifier) == ("B", "executed", "data")
    assert verifier.tasks[-1].skill == "fraud_score"
    high = await gate.execute(
        act("seller_review", "finance", "sel-2", seller_id="sel_2", risk_score=70)
    )
    assert (high.grade, high.status) == ("C", "held")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["create_claim", "decide_claim", "receive_return", "inspect_return"]
)
async def test_support_claim_records_are_grade_a(gate: Gate, act, action: str) -> None:
    """#105: 클레임 처리 기록은 돈이 나가지 않으므로 A. 다른 부서가 요청하면 미등록과 같다."""
    decision = await gate.execute(act(action, "support", f"claim-{action}", claim_id=1))
    assert (decision.grade, decision.status) == ("A", "executed")
    other = await gate.execute(act(action, "marketing", f"claim-{action}-x", claim_id=1))
    assert other.hold_reason == HoldReason.UNREGISTERED_ACTION


@pytest.mark.asyncio
async def test_claim_refund_still_needs_finance_check(gate: Gate, verifier: Any, act) -> None:
    """클레임 환불 지급은 refund_request(B)로 재무가 검증한다."""
    decision = await gate.execute(
        act(
            "refund_request",
            "support",
            "claim-refund",
            claim_id=1,
            order_id=3,
            buyer_id="usr_1",
            refund_amount=20_000,
        )
    )
    assert (decision.grade, decision.status, decision.verifier) == ("B", "executed", "finance")
    assert verifier.tasks[-1].input["params"]["claim_id"] == 1
