import pytest
from pydantic import ValidationError

from ax_contracts.data_access.shop_failed_orders import (
    ShopFailedOrdersQuery,
    ShopFailedOrdersResult,
)
from ax_contracts.schemas import (
    Card,
    ContextPrefix,
    Escalation,
    ExecuteDecision,
    ExecuteRequest,
    HoldReason,
    IdPrefix,
    McpEnvelope,
    RecordCase,
    RecordKind,
)


def test_execute_request_example() -> None:
    request = ExecuteRequest.model_validate_json(
        """{
          "action": "issue_coupon",
          "requester": "marketing",
          "context_id": "ctx_inc_0001",
          "idempotency_key": "ctx_inc_0001:issue_coupon:cpn_1",
          "params": {"count": 3},
          "claimed_grade": "A"
        }"""
    )
    assert request.tenant_id == "shop-demo"
    assert request.claimed_grade == "A"


def test_execute_decision_held_needs_reason() -> None:
    held = ExecuteDecision(
        execution_id="exe_1",
        grade="C",
        status="held",
        hold_reason=HoldReason.OVER_THRESHOLD,
        review_id="rev_1",
    )
    assert held.hold_reason == "over_threshold"
    with pytest.raises(ValidationError, match="hold_reason"):
        ExecuteDecision(execution_id="exe_2", grade="C", status="held")
    with pytest.raises(ValidationError):
        ExecuteDecision(execution_id="exe_3", grade="read", status="executed")


def test_hold_reason_members() -> None:
    assert {r.value for r in HoldReason} == {
        "over_threshold",
        "high_impact",  # 기준과 무관하게 기본 등급이 C인 건 (요청 #180·#431)
        "unregistered_action",
        "amount_mismatch",
        "verify_rejected",
        "verify_timeout",
        "schema_invalid",
        "review_timeout",
    }
    # 기준을 넘은 건과 늘 사람이 정하는 건은 다른 값이다 (화면 문구가 갈린다)
    assert HoldReason.HIGH_IMPACT != HoldReason.OVER_THRESHOLD


def test_record_case_example_and_kinds() -> None:
    record = RecordCase.model_validate_json(
        """{
          "context_id": "ctx_req_0123",
          "kind": "spec_confirmed",
          "summary": "결제 재시도 기준 확정",
          "decision_reason": "중복 결제 위험을 줄이기 위해",
          "alternatives_considered": ["재시도 없음"],
          "source_refs": ["pr:412"],
          "author": "planning"
        }"""
    )
    assert record.kind == RecordKind.SPEC_CONFIRMED
    assert {k.value for k in RecordKind} == {
        "incident",
        "decision",
        "case",
        "notice",
        "rollback_executed",
        "spec_confirmed",
        "pr_merged",
        "decision_extracted",
        "request_closed",
        "decision_rejected",
    }


def test_card_example_and_status_rules() -> None:
    base = {
        "card_id": "card_1",
        "kind": "incident",
        "context_id": "ctx_inc_0001",
        "title": "결제 API 장애",
        "body": "롤백으로 복구",
        "author": "hr",
        "confidence": "high",
        "created_at": "2026-10-22T06:00:00Z",
    }
    card = Card.model_validate(base)
    assert card.status == "active"
    with pytest.raises(ValidationError, match="superseded_by"):
        Card.model_validate({**base, "status": "superseded"})
    with pytest.raises(ValidationError, match="superseded_by"):
        Card.model_validate({**base, "superseded_by": "card_2"})
    assert Card.model_validate({**base, "status": "superseded", "superseded_by": "card_2"})
    with pytest.raises(ValidationError):
        Card.model_validate({**base, "kind": "memo"})
    with pytest.raises(ValidationError):
        Card.model_validate({**base, "confidence": "very-high"})


def test_escalation_and_mcp_envelope() -> None:
    escalation = Escalation(context_id="ctx_inc_1", path=["a", "b", "c", "d"], reason="HOP_LIMIT")
    assert escalation.tenant_id == "shop-demo"
    envelope = McpEnvelope.model_validate_json(
        '{"ok": true, "collected_at": "2026-10-22T06:00:00Z", "data": {"n": 1}}'
    )
    assert envelope.truncated is False


def test_data_access_example() -> None:
    query = ShopFailedOrdersQuery.model_validate(
        {"from": "2026-10-22T05:00:00Z", "to": "2026-10-22T06:00:00Z", "category": "book"}
    )
    assert query.category == "book"
    result = ShopFailedOrdersResult.model_validate(
        {
            "orders": [
                {
                    "order_id": "ord_1",
                    "buyer_id": "buyer_anon_1",
                    "amount": 12000,
                    "category": "book",
                    "failed_at": "2026-10-22T05:30:00Z",
                    "reason": "payment_timeout",
                }
            ],
            "collected_at": "2026-10-22T06:00:00Z",
        }
    )
    assert result.orders[0].tenant_id == "shop-demo"
    assert result.truncated is False
    assert query.limit == 1000


def test_id_prefixes() -> None:
    assert IdPrefix.BUYER == "buy_"
    assert IdPrefix.CARD == "card_"
    assert IdPrefix.CLAIM == "clm_"
    assert {p.value for p in ContextPrefix} == {"ctx_inc_", "ctx_req_", "ctx_ntc_", "ctx_cmp_"}
