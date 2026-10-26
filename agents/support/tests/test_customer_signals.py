"""`customer_signals`·`list_at_risk_customers`: 문의·클레임으로 이탈·재문의 위험을 센다 (읽기)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_agent import claim_handlers, customer_signals, handlers, signal_handlers
from support_agent.claim_schemas import Claim
from support_agent.claims import ClaimStore
from support_agent.main import app, load_card
from support_agent.schemas import Analysis, Classification
from support_agent.store import InquiryStore

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
SECRET = "홍길동 010-9999-8888 환불 안 해주면 신고합니다"


def iso(days_ago: float = 0, hours_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    monkeypatch.setattr(claim_handlers, "CLAIMS", ClaimStore())
    monkeypatch.setattr(signal_handlers, "now", lambda: NOW)


def add_inquiry(
    user_id: int = 1,
    *,
    sentiment: str = "calm",
    days_ago: float = 1,
    hours_since_update: float = 1,
    status: str = "open",
    awaiting: bool = True,
) -> int:
    conversation = handlers.STORE.create(
        context_id="ctx_req_0001",
        tenant_id="shop-demo",
        user_id=user_id,
        customer_name="홍길동",
        subject="문의",
        order_id=None,
        order_no=None,
        first_message=SECRET,
        analysis=Analysis(
            classification=Classification(
                intent_ko="문의",
                category="refund",
                sentiment=sentiment,  # type: ignore[arg-type]
                urgency="medium",
                search_query="refund",
            )
        ),
    )
    conversation.messages[0].created_at = iso(days_ago)
    conversation.updated_at = iso(hours_ago=hours_since_update)
    conversation.status = status  # type: ignore[assignment]
    conversation.awaiting_reply = awaiting
    return conversation.id


def add_claim(buyer_id: str = "buy_0001", *, days_ago: float = 5, status: str = "approved") -> None:
    claim = Claim(
        id=len(claim_handlers.CLAIMS.list()) + 1,
        context_id="ctx_req_0002",
        order_id="ord_0001",
        order_no="ord_0001",
        buyer_id=buyer_id,
        customer="홍길동",
        type="return",
        reason="defect",
        status=status,  # type: ignore[arg-type]
        created_at=iso(days_ago),
        updated_at=iso(days_ago),
        outcome="approve",
        resolution="환불",
        requires_return=False,
    )
    claim_handlers.CLAIMS._claims[claim.id] = claim


def call(skill: str, input: dict[str, Any], requester: str = "dashboard-api") -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_ui_0001",
            "token": f"mock:{requester}",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": skill,
        "input": input,
    }
    return TestClient(app).post("/a2a", json=body).json()


def signals(buyer: str = "buy_0001") -> dict[str, Any]:
    return call("customer_signals", {"buyer_id": buyer})["output"]


def reason_codes(out: dict[str, Any]) -> dict[str, int]:
    return {r["code"]: r["points"] for r in out["reasons"]}


def test_cards_declare_two_read_skills_for_the_dashboard_and_orchestrator() -> None:
    skills = {s.id: s for s in load_card().skills}
    for skill_id in ("customer_signals", "list_at_risk_customers"):
        assert skills[skill_id].grade == "read"
        assert set(skills[skill_id].allowed_requesters) == {"dashboard-api", "orchestrator"}
    refused = call("customer_signals", {"buyer_id": "buy_0001"}, requester="marketing")
    assert refused["status"] == "rejected"


def test_a_buyer_without_history_is_low_and_says_so() -> None:
    result = call("customer_signals", {"buyer_id": "buy_0009"})
    assert result["outcome"] == "no_history"
    out = result["output"]
    assert (out["level"], out["score"], out["recommended_action"]) == ("low", 0, "none")
    assert out["last_contact_at"] is None and out["reasons"] == []


def test_a_calm_single_inquiry_is_low() -> None:
    add_inquiry()
    out = signals()
    assert out["level"] == "low" and out["counts"]["inquiries_30d"] == 1


def test_an_inquiry_unanswered_for_a_day_is_three_points_and_medium() -> None:
    add_inquiry(hours_since_update=30)
    out = signals()
    assert reason_codes(out) == {"unanswered_over_24h": 3}
    assert (out["level"], out["recommended_action"]) == ("medium", "proactive_check")


def test_a_recent_unanswered_inquiry_is_not_counted() -> None:
    add_inquiry(hours_since_update=5)
    assert signals()["counts"]["unanswered_over_24h"] == 0


def test_sentiment_points_are_capped_per_kind() -> None:
    for _ in range(4):
        add_inquiry(sentiment="angry")
    out = signals()
    assert out["counts"]["angry_messages_30d"] == 4
    assert reason_codes(out)["angry_messages"] == 4  # 2건까지만 2점씩


def test_repeat_contact_needs_three_inquiries_in_thirty_days() -> None:
    add_inquiry(days_ago=2)
    add_inquiry(days_ago=3)
    assert "repeat_contact" not in reason_codes(signals())
    add_inquiry(days_ago=4)
    assert reason_codes(signals())["repeat_contact"] == 2
    add_inquiry(days_ago=40)  # 30일 밖은 세지 않는다
    assert signals()["counts"]["inquiries_30d"] == 3


def test_rejected_and_frequent_claims_add_points_and_three_claims_flag_a_pattern() -> None:
    add_claim(status="rejected", days_ago=5)
    add_claim(days_ago=20)
    out = signals()
    assert reason_codes(out) == {"rejected_claim": 2, "frequent_claims": 2}
    assert out["claim_pattern_review"] is False
    add_claim(days_ago=30)
    assert signals()["claim_pattern_review"] is True
    add_claim(days_ago=200)  # 90일 밖은 세지 않는다
    assert signals()["counts"]["claims_90d"] == 3


def test_levels_follow_the_score_table() -> None:
    levels = [customer_signals.level_for(n) for n in (0, 2, 3, 5, 6, 12)]
    assert levels == ["low", "low", "medium", "medium", "high", "high"]


def test_a_stack_of_reasons_reaches_high_with_a_personal_followup() -> None:
    add_inquiry(sentiment="angry", hours_since_update=48)
    add_inquiry(sentiment="angry", days_ago=2)
    add_inquiry(sentiment="frustrated", days_ago=3)
    add_claim(status="rejected")
    out = signals()
    assert out["level"] == "high" and out["recommended_action"] == "personal_followup"
    assert out["score"] == sum(r["points"] for r in out["reasons"])


def test_escalated_inquiries_count_but_are_not_open() -> None:
    add_inquiry(status="escalated", awaiting=False)
    out = signals()
    assert reason_codes(out) == {"escalated_inquiry": 1}
    assert out["counts"]["open_inquiries"] == 0


def test_buyer_ids_in_other_forms_are_normalized() -> None:
    add_inquiry(user_id=1, hours_since_update=30)
    for form in ("buy_0001", "usr_1"):  # 옛 usr_ 형식도 읽는다
        assert signals(form)["buyer_id"] == "buy_0001"
        assert signals(form)["level"] == "medium"


def test_other_buyers_data_is_not_mixed_in() -> None:
    add_inquiry(user_id=2, sentiment="angry", hours_since_update=48)
    add_claim("buy_0002", status="rejected")
    assert signals("buy_0001")["score"] == 0


def test_the_last_contact_is_the_latest_customer_message() -> None:
    add_inquiry(days_ago=9)
    add_inquiry(days_ago=2)
    assert signals()["last_contact_at"] == iso(2)


def test_neither_the_name_nor_the_message_text_leaves_the_agent() -> None:
    add_inquiry(sentiment="angry", hours_since_update=48)
    add_claim(status="rejected")
    text = str(call("customer_signals", {"buyer_id": "buy_0001"})) + str(
        call("list_at_risk_customers", {})
    )
    for leaked in ("홍길동", "010-9999-8888", "신고"):
        assert leaked not in text


def test_the_list_is_sorted_by_score_filtered_by_level_and_limited() -> None:
    add_inquiry(user_id=1, hours_since_update=30)  # 3점 medium
    add_inquiry(user_id=2, sentiment="angry", hours_since_update=48)  # 3 + 2 = 5점 medium
    for _ in range(2):
        add_inquiry(user_id=3, sentiment="angry", hours_since_update=48)  # 3 + 4 = 7점 high
    add_inquiry(user_id=4)  # low는 목록에 없다
    medium = call("list_at_risk_customers", {})["output"]
    assert [i["buyer_id"] for i in medium["items"]] == ["buy_0003", "buy_0002", "buy_0001"]
    assert medium["total"] == 3
    high = call("list_at_risk_customers", {"min_level": "high"})["output"]
    assert [i["buyer_id"] for i in high["items"]] == ["buy_0003"]
    limited = call("list_at_risk_customers", {"limit": 1})["output"]
    assert len(limited["items"]) == 1 and limited["total"] == 3


def test_the_list_includes_buyers_who_only_have_claims() -> None:
    add_claim("buy_0007", status="rejected")
    add_claim("buy_0007", days_ago=9)
    out = call("list_at_risk_customers", {})["output"]
    assert [i["buyer_id"] for i in out["items"]] == ["buy_0007"]


def test_invalid_inputs_are_refused() -> None:
    assert call("list_at_risk_customers", {"limit": 0})["status"] == "failed"
    assert call("list_at_risk_customers", {"min_level": "low"})["status"] == "failed"
    assert call("customer_signals", {})["status"] == "failed"


def test_evidence_names_the_records_that_were_counted() -> None:
    inquiry_id = add_inquiry(hours_since_update=30)
    refs = call("customer_signals", {"buyer_id": "buy_0001"})["evidence"]["ref_ids"]
    assert f"inquiry:{inquiry_id}" in refs
