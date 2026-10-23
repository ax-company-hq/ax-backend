"""목 모드가 아닐 때 클레임·보상 큐·KPI 경로가 support skill을 부르고 화면 모델로 바꾸는지 본다.

응답 dict는 support 에이전트 출력(`claim_schemas.py`)의 실제 모양을 본뜬 것이다.
"""

from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from ax_dashboard_api import support_client
from ax_dashboard_api.main import app

BASE = "/api/dashboard"

RECEIPT = {
    "order_no": "ORD-2026-0101",
    "status": "delivered",
    "items_total": 30000,
    "shipping_fee": 3000,
    "points_used": 0,
    "paid_amount": 33000,
    "paid_at": "2026-10-10T00:00:00Z",
    "delivered_at": "2026-10-14T00:00:00Z",
    "items": [
        {
            "id": 1,
            "title": "고양이의 하루",
            "qty": 1,
            "paid_amount": 15000,
            "points_share": 0,
            "refunded_qty": 0,
        }
    ],
}
CLAIM = {
    "id": 1,
    "tenant_id": "shop-demo",
    "context_id": "ctx_req_0001",
    "order_id": 101,
    "order_no": "ORD-2026-0101",
    "user_id": 1,
    "customer": "김하늘",
    "type": "return",
    "reason": "defect",
    "status": "needs_review",
    "created_at": "2026-10-22T01:00:00Z",
    "updated_at": "2026-10-22T01:00:00Z",
    "outcome": "needs_review",
    "resolution": "환불",
    "requires_return": True,
    "evidence_count": 2,
    "unsealed": False,
    "conversation_id": None,
    "reasons": [
        {
            "code": "DEFECT_EVIDENCE",
            "level": "review",
            "message": "사진 증빙 확인 필요",
            "rule": "refund-2",
        },
        {"code": "HIGH_AMOUNT", "level": "review", "message": "5만 원 이상", "rule": None},
    ],
    "lines": [
        {
            "order_item_id": 1,
            "title": "고양이의 하루",
            "qty": 1,
            "requested_qty": 1,
            "unit_price": 15000,
            "refund_cash": 15000,
            "points_back": 0,
        }
    ],
    "deductions": [{"code": "RETURN_SHIPPING", "amount": 6000, "message": "왕복 배송비"}],
    "shipping_refund": 3000,
    "expected_refund": 33000,
    "points_back": 0,
    "abuse_score": 30,
    "inspection": None,
    "return_received_at": None,
    "refund_paid": 0,
    "processed_at": None,
    "decided_by": None,
    "agent_note": None,
    "inspection_note": None,
    "receipt": RECEIPT,
    "audit": [
        {
            "actor": "support",
            "action": "decide",
            "detail": {"outcome": "needs_review", "abuse_score": 30},
            "at": "2026-10-22T01:00:00Z",
        }
    ],
}
ENTRY = {
    "position": 1,
    "claim_id": 1,
    "order_no": "ORD-2026-0101",
    "customer": "김하늘",
    "type": "return",
    "reason": "defect",
    "received_at": "2026-10-22T03:00:00Z",
    "inspection": None,
    "amount": 33000,
    "ready": False,
    "blockers": [
        {"code": "NOT_INSPECTED", "level": "block", "message": "검수 전이라 보류", "rule": None}
    ],
    "items": [{"title": "고양이의 하루", "qty": 1}],
}


def claim(**changes: Any) -> dict[str, Any]:
    return {**deepcopy(CLAIM), **changes}


class Fake:
    """support 대역. skill별 출력을 `outputs`에 넣어 둔다."""

    def __init__(self) -> None:
        self.sent: list[AgentRequest] = []
        self.outputs: dict[str, dict[str, Any]] = {}
        self.outcome: str | None = None

    async def __call__(self, target: str, req: AgentRequest) -> AgentResult:
        assert target == "support"
        self.sent.append(req)
        return AgentResult(
            meta=req.meta,
            status=TaskStatus.COMPLETED,
            outcome=self.outcome,
            output=self.outputs.get(req.skill, {}),
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "test-token")
    fake = Fake()
    monkeypatch.setattr(support_client, "SEND", fake)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------- 읽기
def test_list_maps_status_rules_and_hides_refund_on_rejected(
    client: TestClient, fake: Fake
) -> None:
    fake.outputs["list_claims"] = {
        "items": [claim(), claim(id=2, status="approved"), claim(id=3, status="rejected")]
    }
    body = client.get(BASE + "/claims").json()
    rows = {c["id"]: c for c in body["data"]}
    assert body["meta"]["total"] == 3
    assert rows[2]["status"] == "return_received"  # 화면에 없는 approved는 처리 대기로 보낸다
    assert [r["code"] for r in rows[1]["reasons"]] == ["DEFECT_EVIDENCE", "OVER_AUTO_LIMIT"]
    assert rows[1]["deductions"][0]["code"] == "RETURN_FEE"
    assert rows[1]["payable"] == 33000 and rows[1]["abuse"]["score"] == 30
    assert rows[3]["payable"] == 0 and rows[3]["expected_refund"] == 0
    assert fake.sent[0].skill == "list_claims" and fake.sent[0].meta.idempotency_key is None


def test_list_filters_on_screen_status(client: TestClient, fake: Fake) -> None:
    fake.outputs["list_claims"] = {"items": [claim(), claim(id=2, status="approved")]}
    ids = [c["id"] for c in client.get(BASE + "/claims?status=return_received").json()["data"]]
    assert ids == [2]


def test_detail_maps_audit_receipt_inspection_and_approval(client: TestClient, fake: Fake) -> None:
    audit = claim()["audit"] + [
        {
            "actor": "support",
            "action": "approve",
            "detail": {"agent_action": "approve"},
            "at": "t1",
        },
        {"actor": "support", "action": "receive", "detail": {}, "at": "t2"},
    ]
    fake.outputs["get_claim"] = claim(
        status="return_received",
        return_received_at="2026-10-22T03:00:00Z",
        audit=audit,
        agent_note="확인함",
    )
    data = client.get(BASE + "/claims/1").json()["data"]
    assert [a["action"] for a in data["audit"]] == ["decided", "agent_override", "received"]
    assert data["inspection"] == "pending"  # 입고했는데 검수 전
    assert data["manager_approved"] is True and data["agent_note"] == "확인함"
    assert data["receipt"]["items"][0]["title"] == "고양이의 하루"
    assert fake.sent[0].input == {"claim_id": 1}


@pytest.mark.parametrize(
    ("support", "screen"), [("pass", "ok"), ("fail", "mismatch"), ("partial", "mismatch")]
)
def test_inspection_values_use_screen_vocabulary(
    client: TestClient, fake: Fake, support: str, screen: str
) -> None:
    fake.outputs["get_claim"] = claim(
        status="return_received", return_received_at="t", inspection=support
    )
    assert client.get(BASE + "/claims/1").json()["data"]["inspection"] == screen


def test_stats_merge_approved_and_map_rule_codes(client: TestClient, fake: Fake) -> None:
    fake.outputs["claim_stats"] = {
        "total": 3,
        "by_status": {"needs_review": 1, "approved": 1, "return_received": 1},
        "rule_hits": {"HIGH_AMOUNT": 2, "DEFECT_EVIDENCE": 1},
        "agent_reviews": 1,
        "agent_overrides": 0,
    }
    data = client.get(BASE + "/claims/stats").json()["data"]
    assert data["by_status"] == {"needs_review": 1, "return_received": 2}
    assert data["rule_hits"] == {"OVER_AUTO_LIMIT": 2, "DEFECT_EVIDENCE": 1}
    assert data["agent_reviews"] == 1 and data["overridden_rules"] == {}


def test_queue_entries_and_awaiting_return(client: TestClient, fake: Fake) -> None:
    ready = {
        **ENTRY,
        "position": 2,
        "claim_id": 2,
        "ready": True,
        "blockers": [],
        "inspection": "pass",
    }
    fake.outputs["claim_queue"] = {
        "queue": [ENTRY, ready],
        "awaiting_return": [claim(status="awaiting_return")],
    }
    body = client.get(BASE + "/queue-entries").json()
    first, second = body["data"]["queue"]
    assert body["meta"]["total"] == 2
    assert first["inspection"] == "pending" and first["blockers"] == [
        {"code": "NOT_INSPECTED", "message": "검수 전이라 보류"}
    ]
    assert second["inspection"] == "ok" and second["compensation"] == 0
    assert body["data"]["awaiting_return"][0]["status"] == "awaiting_return"


def test_kpis_come_from_inquiry_overview(client: TestClient, fake: Fake) -> None:
    fake.outputs["inquiry_overview"] = {
        "kpis": {
            "conversations": 2,
            "by_status": {"open": 2},
            "agent_replies": 1,
            "draft_adoption_rate": 0.5,
            "accepted_as_is": 1,
            "edited": 0,
            "rejected": 1,
            "avg_edit_ratio": None,
            "avg_first_draft_ms": 500,
            "drafts_generated": 3,
            "drafts_blocked_by_guard": 1,
            "guard_findings": {"promise_refund": 1},
            "by_category": {"refund": 1},
            "by_urgency": {"high": 1},
        },
        "waiting": [],
    }
    data = client.get(BASE + "/kpis").json()["data"]
    assert data["draft_adoption_rate"] == 0.5 and data["avg_first_draft_ms"] == 500
    assert "by_category" not in data  # 화면 KPI 모델에 없는 필드는 빼고 준다


# ---------------------------------------------------------------- 쓰기
def test_approve_sends_note_as_untrusted_with_idempotency_key(
    client: TestClient, fake: Fake
) -> None:
    fake.outputs["decide_claim"] = claim(status="awaiting_return", decided_by="agent_console")
    response = client.post(BASE + "/claims/1/approve", json={"note": "사진 확인"})
    assert response.status_code == 200
    sent = fake.sent[0]
    assert sent.skill == "decide_claim"
    assert sent.input == {"claim_id": 1, "decision": "approve"}
    assert sent.untrusted == {"note": "사진 확인"}
    assert sent.meta.idempotency_key == f"{sent.meta.context_id}:decide_claim:claim:1:approve"
    assert response.json()["data"]["status"] == "awaiting_return"


def test_reject_calls_decide_claim(client: TestClient, fake: Fake) -> None:
    fake.outputs["decide_claim"] = claim(status="rejected")
    client.post(BASE + "/claims/1/reject", json={"note": ""})
    assert fake.sent[0].input == {"claim_id": 1, "decision": "reject"}


def test_receive_and_inspection_translate_screen_values(client: TestClient, fake: Fake) -> None:
    fake.outputs["receive_return"] = claim(status="return_received", return_received_at="t")
    fake.outputs["inspect_return"] = claim(
        status="return_received", return_received_at="t", inspection="fail"
    )
    client.post(BASE + "/claims/1/receive", json={"inspection": "ok"})
    client.post(BASE + "/claims/1/inspection", json={"inspection": "mismatch", "note": "다른 상품"})
    receive, inspect = fake.sent
    assert receive.skill == "receive_return" and receive.input["inspection"] == "pass"
    assert inspect.skill == "inspect_return" and inspect.input["inspection"] == "fail"
    assert inspect.untrusted == {"note": "다른 상품"}


def test_unknown_inspection_value_is_422_without_calling_support(
    client: TestClient, fake: Fake
) -> None:
    response = client.post(BASE + "/claims/1/receive", json={"inspection": "bogus"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert fake.sent == []


def test_invalid_state_becomes_409(client: TestClient, fake: Fake) -> None:
    fake.outcome = "invalid_state"
    fake.outputs["decide_claim"] = {"message": "클레임 1는 rejected 상태라 결정할 수 없다"}
    response = client.post(BASE + "/claims/1/approve", json={"note": ""})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_not_found_becomes_404(client: TestClient, fake: Fake) -> None:
    fake.outcome = "not_found"
    fake.outputs["get_claim"] = {"message": "클레임 9를 찾을 수 없다"}
    assert client.get(BASE + "/claims/9").status_code == 404


def test_process_queue_maps_results_and_blockers(client: TestClient, fake: Fake) -> None:
    fake.outputs["process_claim_queue"] = {
        "results": [
            {"claim_id": 1, "result": "paid", "detail": None, "blockers": [], "refund": 12000},
            {
                "claim_id": 2,
                "result": "held",
                "detail": "verifying",
                "refund": None,
                "blockers": [
                    {
                        "code": "ON_HOLD",
                        "level": "block",
                        "message": "재무 교차검증 중",
                        "rule": None,
                    }
                ],
            },
        ],
        "paid": 1,
        "held": 1,
        "queue": [ENTRY],
    }
    response = client.post(BASE + "/queue-entries/process", json={"limit": 5})
    data = response.json()["data"]
    assert data["paid"] == 1 and data["held"] == 1
    assert data["results"][0] == {
        "claim_id": 1,
        "result": "paid",
        "blockers": [],
        "refund": 12000,
        "compensation": 0,
    }
    assert data["results"][1]["blockers"] == [{"code": "ON_HOLD", "message": "재무 교차검증 중"}]
    assert fake.sent[0].input == {"limit": 5, "claim_ids": None}


def test_process_queue_without_body(client: TestClient, fake: Fake) -> None:
    fake.outputs["process_claim_queue"] = {"results": [], "paid": 0, "held": 0, "queue": []}
    assert client.post(BASE + "/queue-entries/process").status_code == 200
    assert fake.sent[0].input == {"limit": None, "claim_ids": None}


def test_detail_without_receipt_is_a_bad_gateway(client: TestClient, fake: Fake) -> None:
    fake.outputs["get_claim"] = claim(receipt=None)
    response = client.get(BASE + "/claims/1")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
