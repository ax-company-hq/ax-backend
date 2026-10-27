"""등급 판정 테스트 (재무 구성안 8장 단독 테스트 ①②③④⑥, SAF-03·04·05·06·10).

A/B/C 등급, 금액·누적 한도, 환불, 미등록·스키마, 멱등성, 감사 기록.
"""

from __future__ import annotations

from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate


@pytest.mark.asyncio
async def test_grade_a_executes_immediately(gate: Gate, sent: Any, act) -> None:
    decision = await gate.execute(act("publish_notice", "marketing", "k1", draft_id="d1"))
    assert (decision.grade, decision.status) == ("A", "executed")
    assert decision.execution_id.startswith("exe_")
    assert sent.types() == ["safety.action_executed"]


@pytest.mark.asyncio
async def test_claimed_grade_is_ignored(gate: Gate, verifier: Any, coupon) -> None:
    """① 등급은 규칙이 정한다: `claimed_grade="C"`여도 50,000원은 A, `"A"`여도 400,000원은 C."""
    small = await gate.execute(coupon(10, 5000, purpose_code="campaign", claimed_grade="C"))
    assert (small.grade, small.status, small.verifier) == ("A", "executed", None)
    assert small.computed_amount == 50_000
    assert verifier.tasks == []  # 쿠폰은 재무 교차검증을 하지 않는다 (재무 재설계)

    big = await gate.execute(coupon(20, 20_000, purpose_code="campaign", claimed_grade="A"))
    assert (big.grade, big.status, big.hold_reason) == ("C", "held", HoldReason.OVER_THRESHOLD)
    assert big.review_id and big.review_id.startswith("rev_")
    assert verifier.tasks == []  # C는 검증자에게 보내지 않는다


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
async def test_idempotent_replay(gate: Gate, verifier: Any, sent: Any, seller_review) -> None:
    """④ 같은 멱등성 키 두 번 → 판정·검증·이벤트는 한 번, 두 번째는 이전 결과."""
    first = await gate.execute(seller_review("same"))
    second = await gate.execute(seller_review("same"))
    assert first == second
    assert len(verifier.tasks) == 1
    assert sent.types() == ["safety.action_executed"]
    assert [e.step for e in gate.store.list_audit()][-1] == "idempotent_replay"


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
    # 금액 기준이 없는 C다. 사유는 `over_threshold`로 나가고(응답 enum을 늘리지 않는다),
    # 보고서 `rules`의 `base_grade` 표시로 "늘 사람이 정함"을 구분한다 (요청 #180·#431)
    assert decision.hold_reason == HoldReason.OVER_THRESHOLD
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None
    assert review.report["rules"] == [{"key": "base_grade", "value": "C"}]


@pytest.mark.asyncio
async def test_base_grade_c_is_told_apart_by_the_report_not_the_reason(
    gate: Gate, verifier: Any, act, coupon
) -> None:
    """같은 C·같은 사유라도 보고서로 갈린다: 기준 초과는 규칙 hit, 기본 등급 C는 `base_grade`.

    사람이 읽는 쪽(대시보드)은 이 표시를 보고 "항상 사람이 확인"으로 적는다. 사유를 쪼개려면
    응답 enum이 아니라 새 선택 필드로 더해야 한다 (`high_impact` 추가는 하위 호환이 깨진다).
    """
    over = await gate.execute(coupon(80, 5000, purpose_code="campaign"))  # 400,000원 = 기준 초과
    always = await gate.execute(act("confirm_settlement", "finance", "k-set2", draft="s2"))
    assert (over.grade, always.grade) == ("C", "C")
    assert over.hold_reason == always.hold_reason == HoldReason.OVER_THRESHOLD
    over_rules = gate.store.get_review(over.review_id or "").report["rules"]
    always_rules = gate.store.get_review(always.review_id or "").report["rules"]
    assert [h["key"] for h in always_rules] == ["base_grade"]
    assert "base_grade" not in [h["key"] for h in over_rules]
    assert over.computed_amount == 400_000 and always.computed_amount is None


@pytest.mark.asyncio
async def test_refund_threshold(gate: Gate, verifier: Any, act) -> None:
    small = await gate.execute(act("refund_request", "support", "r1", refund_amount=99_999))
    assert (small.grade, small.status) == ("A", "executed")
    big = await gate.execute(act("refund_request", "support", "r2", refund_amount=100_000))
    assert big.status == "held"
    assert verifier.tasks == []  # 환불은 재무 교차검증을 하지 않는다


@pytest.mark.asyncio
async def test_every_decision_is_audited_with_context(gate: Gate, coupon, seller_review) -> None:
    """SAF-06: 접수·(검증)·판정이 context_id와 함께 남는다. 쿠폰은 A라 검증 단계가 없다."""
    await gate.execute(coupon())
    steps = [(e.step, e.context_id) for e in gate.store.list_audit("ctx_inc_0001")]
    assert [s for s, _ in steps] == ["received", "decided"]
    await gate.execute(seller_review("audit"))
    steps = [e.step for e in gate.store.list_audit("ctx_inc_0001")][2:]
    assert steps == ["received", "verify_requested", "verify_result", "decided"]


@pytest.mark.asyncio
async def test_rejected_and_held_amounts_do_not_count_toward_limits(
    gate: Gate, bonus_gate: Gate, verifier: Any, coupon, bonus
) -> None:
    """누적 한도는 실제로 실행된 금액만 센다. 거절·보류된 금액은 쪼개기 계산에 넣지 않는다.

    쿠폰은 A라 검증자 거절이 없으므로, 거절은 테스트 전용 B 액션 `pay_bonus`
    (일일 누적 100만 원 이상 C)로, 보류는 총액 불일치로 C가 된 쿠폰으로 확인한다.
    """
    verifier.result = "rejected"
    for i in range(4):
        await bonus_gate.execute(bonus(25, 10_000, key=f"rej-{i}"))
    verifier.result = "approved"
    decision = await bonus_gate.execute(bonus(25, 10_000, key="after"))
    assert decision.status == "executed"

    for i in range(4):
        held = coupon(25, 10_000, purpose_code="campaign", key=f"held-{i}")
        held.params["claimed_total"] = 1
        assert (await gate.execute(held)).status == "held"
    after = await gate.execute(coupon(25, 10_000, purpose_code="campaign", key="after-held"))
    assert after.status == "executed"


@pytest.mark.asyncio
async def test_grade_a_coupon_still_escalates_on_rules(gate: Gate, verifier: Any, coupon) -> None:
    """A가 되어도 금액 규칙은 그대로: 총액 불일치·한도 초과·잘못된 금액 필드는 C."""
    ok = await gate.execute(coupon(2, 5000, purpose_code="campaign", key="a-ok"))
    assert (ok.grade, ok.status) == ("A", "executed")

    mismatch = coupon(2, 5000, purpose_code="campaign", key="a-mismatch")
    mismatch.params["claimed_total"] = 1_000
    held = await gate.execute(mismatch)
    assert (held.grade, held.status, held.hold_reason) == (
        "C",
        "held",
        HoldReason.AMOUNT_MISMATCH,
    )

    over = await gate.execute(coupon(15, 20_000, purpose_code="campaign", key="a-over"))
    assert (over.status, over.hold_reason) == ("held", HoldReason.OVER_THRESHOLD)

    customer = await gate.execute(coupon(5, 10_000, key="a-customer"))  # 고객당 보상 5만 원
    assert (customer.status, customer.hold_reason) == ("held", HoldReason.OVER_THRESHOLD)

    bad = coupon(1, 5000, purpose_code="campaign", key="a-bad")
    bad.params["unit_value"] = "5000"
    invalid = await gate.execute(bad)
    assert (invalid.status, invalid.hold_reason) == ("held", HoldReason.SCHEMA_INVALID)
    assert verifier.tasks == []


@pytest.mark.asyncio
async def test_refund_amount_rules_still_apply(gate: Gate, verifier: Any, act) -> None:
    """환불 A: 금액 필드가 없거나 정수가 아니면 C(`schema_invalid`), 10만 원 이상은 C."""
    missing = await gate.execute(act("refund_request", "support", "rf-none", order_id="ord_1"))
    assert (missing.status, missing.hold_reason) == ("held", HoldReason.SCHEMA_INVALID)
    text = await gate.execute(act("refund_request", "support", "rf-str", refund_amount="9000"))
    assert text.hold_reason == HoldReason.SCHEMA_INVALID
    big = await gate.execute(act("refund_request", "support", "rf-big", refund_amount=150_000))
    assert (big.grade, big.status, big.hold_reason) == ("C", "held", HoldReason.OVER_THRESHOLD)
    review = gate.store.get_review(big.review_id or "")
    assert review is not None and review.report["rules"][0]["key"] == "refund_single"
    assert verifier.tasks == []


@pytest.mark.asyncio
async def test_same_key_different_tenant_is_separate(gate: Gate, coupon) -> None:
    a = await gate.execute(coupon(key="shared-key"))
    b = await gate.execute(coupon(key="shared-key").model_copy(update={"tenant_id": "other-shop"}))
    assert a.execution_id != b.execution_id
