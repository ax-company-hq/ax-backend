"""부서별 등록 액션 테스트: 회사 기억 카드, 고객지원 클레임, generate_pr 코드 규칙, 판매자 승인."""

from __future__ import annotations

from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate

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
