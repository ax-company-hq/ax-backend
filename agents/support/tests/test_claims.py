from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import ExecuteDecision
from support_agent import claim_handlers, claim_rules, handlers, orders
from support_agent.claims import ClaimStore
from support_agent.main import app
from support_agent.store import InquiryStore

NOW = datetime(2026, 10, 18, tzinfo=UTC)  # 주문 101 수령(10/14) 4일 뒤


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    monkeypatch.setattr(claim_handlers, "CLAIMS", ClaimStore())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def call(
    client: TestClient,
    skill: str,
    input: dict[str, Any] | None = None,
    untrusted: dict[str, Any] | None = None,
    key: str | None = None,
    context: str = "ctx_req_0001",
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": context,
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input or {},
        "untrusted": untrusted or {},
    }
    return client.post("/a2a", json=body).json()


def create(client: TestClient, key: str = "k-1", **fields: Any) -> dict[str, Any]:
    payload = {
        "user_id": 1,
        "customer_name": "김하늘",
        "order_id": 101,
        "type": "return",
        "reason": "defect",
        "evidence_count": 2,
        **fields,
    }
    return call(client, "create_claim", payload, {"description": "표지가 찢어져 왔어요"}, key=key)


# ---------------------------------------------------------------- 규칙 (순수 코드)
def decide(order_id: int = 101, claim_type: str = "return", reason: str = "defect", **kw: Any):
    defaults: dict[str, Any] = {
        "items": None,
        "evidence_count": 1,
        "unsealed": False,
        "prior_claims": 0,
        "now": NOW,
    }
    return claim_rules.decide(orders.get_order(order_id), claim_type, reason, **{**defaults, **kw})


def test_defect_with_evidence_needs_review_and_refunds_seller_fault_shipping() -> None:
    d = decide()
    assert d.outcome == "needs_review"
    assert d.expected_refund == 30000 + 3000  # 품목 합 + 판매자 부담 배송비
    assert [r.code for r in d.reasons] == ["DEFECT_EVIDENCE"]


def test_defect_without_evidence_needs_info() -> None:
    assert decide(evidence_count=0).outcome == "needs_info"


def test_change_of_mind_within_window_deducts_round_trip_shipping() -> None:
    d = decide(reason="change_of_mind")
    assert d.outcome == "approved_pending_return"
    assert d.deductions == [("RETURN_SHIPPING", 6000, "단순 변심 왕복 배송비 고객 부담")]
    assert d.expected_refund == 30000 - 6000


def test_change_of_mind_after_window_is_auto_rejected() -> None:
    d = decide(reason="change_of_mind", now=NOW + timedelta(days=10))
    assert d.outcome == "auto_rejected"
    assert d.reasons[0].code == "RETURN_WINDOW"


def test_unsealed_change_of_mind_needs_review() -> None:
    assert decide(reason="change_of_mind", unsealed=True).outcome == "needs_review"


def test_cancel_before_shipping_auto_approved_with_points_back() -> None:
    d = decide(order_id=102, claim_type="cancel", reason="change_of_mind")
    assert d.outcome == "auto_approved"
    assert d.requires_return is False
    assert d.expected_refund == 9000 + 3000
    assert d.points_back == 1000


def test_cancel_after_delivery_auto_rejected() -> None:
    assert decide(claim_type="cancel", reason="change_of_mind").outcome == "auto_rejected"


def test_return_before_delivery_auto_rejected() -> None:
    d = decide(order_id=102, reason="defect")
    assert d.outcome == "auto_rejected"
    assert d.reasons[0].code == "NOT_DELIVERED"


def test_exchange_moves_no_money() -> None:
    d = decide(claim_type="exchange", reason="wrong_item")
    assert d.expected_refund == 0
    assert d.resolution == "교환"


def test_partial_quantity_is_computed_from_order_not_request() -> None:
    d = decide(order_id=102, claim_type="cancel", reason="change_of_mind", items={3: 1})
    assert d.lines[0].qty == 1
    assert d.lines[0].refund_cash == 4500  # 9000 // 2 단가


def test_requested_quantity_is_clamped_to_refundable() -> None:
    d = decide(order_id=102, claim_type="cancel", reason="change_of_mind", items={3: 99})
    assert d.lines[0].qty == 2 and d.lines[0].requested_qty == 99


def test_frequent_claimant_goes_to_human_review() -> None:
    d = decide(order_id=102, claim_type="cancel", reason="change_of_mind", prior_claims=3)
    assert d.outcome == "needs_review"
    assert d.abuse_score == 90


# ---------------------------------------------------------------- skill
def test_create_claim_stores_decision_and_audit(client: TestClient) -> None:
    result = create(client)
    assert result["status"] == "completed"
    claim = result["output"]
    assert claim["status"] == "needs_review"
    assert claim["expected_refund"] > 0
    assert claim["audit"][0]["action"] == "decide"
    assert claim["tenant_id"] == "shop-demo"
    assert claim["context_id"] == "ctx_req_0001"


def test_create_claim_ignores_amount_in_input(client: TestClient) -> None:
    result = create(client, expected_refund=999999, refund_amount=1)
    assert result["status"] == "failed"  # 모르는 필드는 거부한다


def test_create_claim_unknown_or_foreign_order_not_found(client: TestClient) -> None:
    assert create(client, order_id=999)["outcome"] == "not_found"
    assert create(client, key="k-2", user_id=2)["outcome"] == "not_found"  # 남의 주문


def test_duplicate_open_claim_is_refused(client: TestClient) -> None:
    create(client)
    assert create(client, key="k-2")["outcome"] == "duplicate_claim"


def test_create_claim_is_idempotent(client: TestClient) -> None:
    first = create(client, key="same")
    second = create(client, key="same")
    assert first["output"]["id"] == second["output"]["id"]
    assert call(client, "claim_stats")["output"]["total"] == 1


def test_free_text_only_travels_in_untrusted(client: TestClient) -> None:
    result = create(client)
    assert "표지가" not in str(result["output"])  # 설명은 저장하지 않고 지시로도 쓰지 않는다


def test_full_return_flow_to_payout(client: TestClient) -> None:
    claim_id = create(client)["output"]["id"]
    approved = call(
        client,
        "decide_claim",
        {"claim_id": claim_id, "decision": "approve"},
        {"note": "확인"},
        key="k-d",
    )["output"]
    assert approved["status"] == "awaiting_return"
    assert call(client, "claim_queue")["output"]["awaiting_return"][0]["id"] == claim_id

    received = call(client, "receive_return", {"claim_id": claim_id}, key="k-r")["output"]
    assert received["status"] == "return_received"
    queue = call(client, "claim_queue")["output"]["queue"]
    assert queue[0]["ready"] is False  # 검수 전

    blocked = call(client, "process_claim_queue", {}, key="k-p0")["output"]
    assert blocked["paid"] == 0 and blocked["results"][0]["result"] == "skipped"

    call(client, "inspect_return", {"claim_id": claim_id, "inspection": "pass"}, key="k-i")
    run = call(client, "process_claim_queue", {}, key="k-p1")["output"]
    assert run["paid"] == 1 and run["results"][0]["refund"] == 33000
    final = call(client, "get_claim", {"claim_id": claim_id})["output"]
    assert final["status"] == "completed"
    assert final["refund_paid"] == 33000
    assert [a["action"] for a in final["audit"]] == [
        "decide",
        "approve",
        "receive",
        "inspection",
        "pay",
    ]
    assert call(client, "claim_queue")["output"]["queue"] == []


def test_failed_inspection_keeps_claim_out_of_payout(client: TestClient) -> None:
    claim_id = create(client)["output"]["id"]
    call(client, "decide_claim", {"claim_id": claim_id, "decision": "approve"}, key="k-d")
    call(client, "receive_return", {"claim_id": claim_id, "inspection": "fail"}, key="k-r")
    run = call(client, "process_claim_queue", {}, key="k-p")["output"]
    assert run["paid"] == 0
    assert "검수 fail" in run["results"][0]["detail"]


def test_reject_and_invalid_transitions(client: TestClient) -> None:
    claim_id = create(client)["output"]["id"]
    assert (
        call(client, "receive_return", {"claim_id": claim_id}, key="k-1")["outcome"]
        == "invalid_state"
    )
    rejected = call(client, "decide_claim", {"claim_id": claim_id, "decision": "reject"}, key="k-2")
    assert rejected["output"]["status"] == "rejected"
    again = call(client, "decide_claim", {"claim_id": claim_id, "decision": "approve"}, key="k-3")
    assert again["outcome"] == "invalid_state"
    assert call(client, "get_claim", {"claim_id": 999})["outcome"] == "not_found"


def test_auto_approved_cancel_goes_straight_to_queue(client: TestClient) -> None:
    result = create(client, order_id=102, user_id=2, type="cancel", reason="change_of_mind")
    assert result["output"]["status"] == "approved"
    run = call(client, "process_claim_queue", {}, key="k-p")["output"]
    assert run["paid"] == 1 and run["results"][0]["refund"] == 12000


def test_stats_count_statuses_and_rules(client: TestClient) -> None:
    create(client)
    create(client, key="k-2", order_id=102, user_id=2, type="cancel", reason="change_of_mind")
    stats = call(client, "claim_stats")["output"]
    assert stats["total"] == 2
    assert stats["by_status"] == {"needs_review": 1, "approved": 1}
    assert stats["rule_hits"] == {"DEFECT_EVIDENCE": 1}


def test_unexecuted_write_does_not_change_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def held(*args: Any, **kwargs: Any) -> ExecuteDecision:
        return ExecuteDecision(
            execution_id="exe_1",
            grade="C",
            status="held",
            hold_reason="unregistered_action",
            review_id="rev_1",
        )

    monkeypatch.setattr(handlers, "execute", held)
    result = create(client)
    assert result["outcome"] == "not_executed_held"
    assert call(client, "list_claims")["output"]["items"] == []


def test_refund_request_not_executed_is_held_and_unpaid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(client, order_id=102, user_id=2, type="cancel", reason="change_of_mind")

    async def verifying(*args: Any, **kwargs: Any) -> ExecuteDecision:
        return ExecuteDecision(execution_id="exe_1", grade="B", status="verifying")

    monkeypatch.setattr(claim_handlers, "execute", verifying)
    run = call(client, "process_claim_queue", {}, key="k-p")["output"]
    assert run["held"] == 1 and run["paid"] == 0
    assert run["results"][0] == {
        "claim_id": 1,
        "result": "held",
        "detail": "verifying",
        "refund": None,
    }
    assert call(client, "get_claim", {"claim_id": 1})["output"]["status"] == "approved"


def test_refund_request_params_use_code_computed_amount(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(client, order_id=102, user_id=2, type="cancel", reason="change_of_mind")
    seen: dict[str, Any] = {}

    async def spy(action: str, params: dict, meta: Any, evidence: Any) -> ExecuteDecision:
        seen.update(action=action, params=params, key=meta.idempotency_key)
        return ExecuteDecision(execution_id="exe_1", grade="B", status="executed")

    monkeypatch.setattr(claim_handlers, "execute", spy)
    call(client, "process_claim_queue", {}, key="k-p", context="ctx_req_0009")
    assert seen["action"] == "refund_request"
    assert seen["params"]["refund_amount"] == 12000
    assert seen["key"] == "ctx_req_0009:refund_request:claim:1"
