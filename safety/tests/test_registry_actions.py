"""부서별 등록 액션 테스트: 회사 기억 카드, 고객지원 클레임, 판매자 승인,
기획(generate_pr·merge_pr·confirm_spec) 코드 규칙과 보호 경로."""

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
@pytest.mark.parametrize(
    "action", ["create_ticket", "write_spec", "split_tasks", "review_pr", "send_message"]
)
async def test_planning_record_actions_are_a(gate: Gate, act, action: str) -> None:
    decision = await gate.execute(act(action, "planning", f"plan-{action}"))
    assert (decision.grade, decision.status) == ("A", "executed")


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", ["support", "hr", "data"])
async def test_receive_change_result_is_a(gate: Gate, act, requester: str) -> None:
    decision = await gate.execute(act("receive_change_result", requester, f"rcr-{requester}"))
    assert (decision.grade, decision.status) == ("A", "executed")


@pytest.mark.asyncio
async def test_receive_change_result_refuses_other_requesters(gate: Gate, act) -> None:
    decision = await gate.execute(act("receive_change_result", "marketing", "rcr-marketing"))
    assert decision.status != "executed"


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
        "merge_allowed_paths": {"changed_paths": ["scripts/x.sh"]},
        "merge_protected_paths": {"changed_paths": ["shop-api/src/ax_shop_api/service.py"]},
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
        "pr_protected_paths",
    }


_PR_CLEAN = dict(changed_lines=10, changed_files=1)
_MERGE_CLEAN = dict(
    changed_lines=10,
    changed_files=1,
    review_mismatch_count=0,
    linked_incident_count=0,
    irreversible_count=0,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "key", "clean"),
    [
        ("generate_pr", "pr_protected_paths", _PR_CLEAN),
        ("merge_pr", "merge_protected_paths", _MERGE_CLEAN),
    ],
)
async def test_protected_path_is_always_c(
    gate: Gate, verifier: Any, act, action: str, key: str, clean: dict[str, int]
) -> None:
    """허용 경로·크기 안이고 irreversible_count가 0이어도 보호 경로에 닿으면 C."""
    protected = "shop-api/src/ax_shop_api/service.py"
    decision = await gate.execute(
        act(action, "planning", f"{key}-hit", changed_paths=["docs/a.md", protected], **clean)
    )
    assert (decision.grade, decision.status) == ("C", "held")
    assert verifier.tasks == []
    review = gate.store.get_review(decision.review_id or "")
    assert review is not None
    [hit] = review.report["rules"]
    assert (hit["key"], hit["protected_hit"]) == (key, [protected])
    assert "shop-web/src/pages/Checkout.tsx" in hit["protected"]

    unrelated = await gate.execute(
        act(
            action,
            "planning",
            f"{key}-ok",
            changed_paths=["shop-api/src/ax_shop_api/lookups.py", "docs/a.md"],
            **clean,
        )
    )
    assert (unrelated.grade, unrelated.status) == ("B", "executed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "key", "clean"),
    [
        ("generate_pr", "pr_protected_paths", _PR_CLEAN),
        ("merge_pr", "merge_protected_paths", _MERGE_CLEAN),
    ],
)
async def test_protected_paths_fail_closed(
    gate: Gate, act, action: str, key: str, clean: dict[str, int]
) -> None:
    """changed_paths가 없거나 문자열 목록이 아니면 보호 경로 규칙도 걸린 것으로 본다."""
    for suffix, extra in {"none": {}, "str": {"changed_paths": "docs/a.md"}}.items():
        decision = await gate.execute(act(action, "planning", f"{key}-{suffix}", **clean, **extra))
        assert decision.status == "held"
        review = gate.store.get_review(decision.review_id or "")
        hits = {hit["key"]: hit for hit in review.report["rules"]}  # type: ignore[union-attr]
        assert hits[key] == {"key": key, "invalid": "changed_paths"}


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


# ---- 롤백 숫자 기준 (SCN-A). 실제 기준표 추가는 사람 PR이라 여기서는 테스트 전용 표로 확인한다
_ROLLBACK_RULES = [
    {"key": "rollback_multiple_suspects", "field": "suspect_count", "op": ">", "value": 1},
    {"key": "rollback_low_confidence", "field": "low_confidence", "op": ">", "value": 0},
    {"key": "rollback_irreversible", "field": "irreversible_count", "op": ">", "value": 0},
    {"key": "rollback_repeat", "field": "prior_rollbacks", "op": ">", "value": 0},
]
_ROLLBACK_OK = {
    "suspect_count": 1,
    "low_confidence": 0,
    "irreversible_count": 0,
    "prior_rollbacks": 0,
}


@pytest.fixture
def rollback_gate(verifier: Any, sent: Any, clock: Any) -> Gate:
    from ax_safety.rules import Rule, load_registry, load_thresholds
    from ax_safety.store import Store

    table = load_thresholds()
    extra = [
        Rule(action="rollback_deployment", metric="param", **rule)  # type: ignore[arg-type]
        for rule in _ROLLBACK_RULES
    ]
    table = table.model_copy(update={"rules": [*table.rules, *extra]})
    return Gate(load_registry(), table, Store(), verifier, sent, now=clock)


@pytest.mark.parametrize(
    ("field", "value", "key"),
    [
        ("suspect_count", 2, "rollback_multiple_suspects"),
        ("low_confidence", 1, "rollback_low_confidence"),
        ("irreversible_count", 1, "rollback_irreversible"),
        ("prior_rollbacks", 1, "rollback_repeat"),
    ],
)
@pytest.mark.asyncio
async def test_rollback_param_rules_escalate_to_c(
    rollback_gate: Gate, verifier: Any, act, field: str, value: int, key: str
) -> None:
    params = {**_ROLLBACK_OK, field: value}
    decision = await rollback_gate.execute(
        act("rollback_deployment", "infra", f"rb-{key}", **params)
    )
    assert (decision.grade, decision.status) == ("C", "held")
    assert decision.hold_reason == HoldReason.OVER_THRESHOLD
    review = rollback_gate.store.get_review(decision.review_id or "")
    assert review is not None
    assert [hit["key"] for hit in review.report["rules"]] == [key]
    assert verifier.tasks == []  # C로 올라가면 검증자를 부르지 않는다


@pytest.mark.parametrize(
    "field", ["suspect_count", "low_confidence", "irreversible_count", "prior_rollbacks"]
)
@pytest.mark.parametrize("bad", ["missing", "1", 1.5, True, -1, None])
@pytest.mark.asyncio
async def test_rollback_param_rules_fail_closed(
    rollback_gate: Gate, verifier: Any, act, field: str, bad: Any
) -> None:
    params: dict[str, Any] = dict(_ROLLBACK_OK)
    if bad == "missing":
        params.pop(field)
    else:
        params[field] = bad
    decision = await rollback_gate.execute(
        act("rollback_deployment", "infra", f"rb-bad-{field}-{bad!r}", **params)
    )
    assert (decision.grade, decision.status) == ("C", "held")
    review = rollback_gate.store.get_review(decision.review_id or "")
    assert review is not None
    assert {
        "key": next(r["key"] for r in _ROLLBACK_RULES if r["field"] == field),
        "invalid": field,
    } in review.report["rules"]
    assert verifier.tasks == []


@pytest.mark.asyncio
async def test_rollback_all_clear_goes_to_b_verification(
    rollback_gate: Gate, verifier: Any, act
) -> None:
    decision = await rollback_gate.execute(
        act("rollback_deployment", "infra", "rb-clear", **_ROLLBACK_OK)
    )
    assert (decision.grade, decision.status) == ("B", "executed")
    assert [(t.verifier, t.skill) for t in verifier.tasks] == [
        ("planning", "verify_rollback_target")
    ]
