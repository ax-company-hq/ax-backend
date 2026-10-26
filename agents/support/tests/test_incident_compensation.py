"""`request_incident_compensation`: 장애 피해 고객 전원에게 보상 쿠폰을 요청한다 (SCN-A 8단계)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from support_agent import compensation, handlers
from support_agent.main import app, load_card
from support_agent.schemas import Analysis
from support_agent.store import InquiryStore

CTX = "ctx_inc_0001"
FOUR = ["buy_0001", "buy_0002", "buy_0003", "buy_0004"]


class Sent(list):
    """마케팅으로 보낸 요청 목록. `replies`에 넣은 결과를 차례로 돌려준다."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[AgentResult] = []


@pytest.fixture(autouse=True)
def sent(monkeypatch: pytest.MonkeyPatch) -> Sent:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    monkeypatch.setattr(handlers, "LEDGER", compensation.SequenceLedger())
    record = Sent()

    async def fake_send(target: str, req: AgentRequest) -> AgentResult:
        assert target == "marketing"
        record.append(req)
        if record.replies:
            return record.replies.pop(0)
        return AgentResult(
            meta=req.meta,
            status=TaskStatus.COMPLETED,
            output={"coupon_id": f"cpn_{len(record):04d}", "count": req.input["count"]},
        )

    monkeypatch.setattr(handlers, "SEND", fake_send)
    return record


def call(
    buyers: list[str],
    *,
    context: str = CTX,
    requester: str = "orchestrator",
    **fields: Any,
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": context,
            "token": f"mock:{requester}",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "request_incident_compensation",
        "input": {"buyer_ids": buyers, **fields},
    }
    return TestClient(app).post("/a2a", json=body).json()


def reply(
    status: TaskStatus,
    output: dict[str, Any] | None = None,
    outcome: str | None = None,
    reason_code: str | None = None,
):
    return AgentResult(
        meta=AgentRequest.model_validate(
            {
                "meta": {
                    "context_id": CTX,
                    "token": "t",
                    "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                },
                "skill": "issue_coupon",
            }
        ).meta,
        status=status,
        output=output or {},
        outcome=outcome,
        reason_code=reason_code,
    )


def test_card_declares_a_required_write_skill_for_the_orchestrator_only() -> None:
    skill = {s.id: s for s in load_card().skills}["request_incident_compensation"]
    assert skill.grade == "A" and skill.priority == "required"
    assert skill.allowed_requesters == ["orchestrator"]
    assert call(FOUR, requester="dashboard-api")["status"] == "rejected"
    assert call(FOUR, requester="marketing")["status"] == "rejected"


def test_every_affected_buyer_gets_one_coupon_request_in_order(sent: Sent) -> None:
    result = call(FOUR, impact_ref="impact:ctx_inc_0001")
    assert result["status"] == "completed" and result["outcome"] == "requested"
    out = result["output"]
    assert out["requested"] == 4 and out["stopped_early"] is False
    assert [r["buyer_id"] for r in out["results"]] == FOUR
    assert [r["sequence"] for r in out["results"]] == [1, 2, 3, 4]
    assert [r["status"] for r in out["results"]] == ["requested"] * 4
    assert [r["coupon_id"] for r in out["results"]] == [f"cpn_000{n}" for n in (1, 2, 3, 4)]
    assert all(r["total_value"] == 2000 for r in out["results"])
    assert [req.skill for req in sent] == ["issue_coupon"] * 4
    first = sent[0]
    assert first.input == {
        "purpose_code": "compensation",
        "buyer_id": "buy_0001",
        "count": 1,
        "unit_value": 2000,
        "claimed_total": 2000,
    }
    assert first.meta.context_id == CTX
    refs = result["evidence"]["ref_ids"]
    assert "impact:ctx_inc_0001" in refs and "buyer:buy_0004" in refs


def test_buyers_who_never_wrote_an_inquiry_are_compensated_too(sent: Sent) -> None:
    assert handlers.STORE.list() == []  # 문의가 하나도 없다
    assert call(["buy_0007"])["output"]["requested"] == 1
    assert sent[0].input["buyer_id"] == "buy_0007"


def test_ids_are_normalized_and_duplicates_are_sent_once(sent: Sent) -> None:
    out = call(["usr_1", "buy_0001", "buy_0002", "buy_0002"])["output"]
    assert [r["buyer_id"] for r in out["results"]] == ["buy_0001", "buy_0002"]
    assert len(sent) == 2


@pytest.mark.parametrize("bad", ["", "buy_1", "buy_12345", "ord_0001", "홍길동", "buy_0001; drop"])
def test_a_malformed_buyer_id_rejects_the_whole_request(sent: Sent, bad: str) -> None:
    result = call(["buy_0001", bad])
    assert result["status"] == "failed" and result["outcome"] == "invalid_input"
    assert sent == []  # 일부만 보내지 않는다
    assert bad not in str(result) or bad == ""  # 오류에 입력값을 싣지 않는다


def test_only_incident_contexts_are_accepted(sent: Sent) -> None:
    for context in ("ctx_inq_0001", "ctx_ui_0001", "ctx_req_0001", "ctx_ntc_0001"):
        result = call(FOUR, context=context)
        assert result["status"] == "failed" and result["outcome"] == "invalid_input", context
    assert sent == []


def test_only_the_payment_failure_reason_and_sane_counts_are_accepted(sent: Sent) -> None:
    assert call(FOUR, reason_code="late_delivery")["status"] == "failed"
    assert call(FOUR, count=0)["status"] == "failed"
    assert call(FOUR, count=4)["status"] == "failed"
    assert call([])["status"] == "failed"
    assert call([f"buy_{n:04d}" for n in range(51)])["status"] == "failed"
    assert sent == []


def test_the_same_request_twice_sends_each_buyer_once(sent: Sent) -> None:
    call(FOUR)
    again = call(FOUR)["output"]
    assert [r["status"] for r in again["results"]] == ["duplicate"] * 4
    assert again["requested"] == 0 and len(sent) == 4


def test_a_buyer_already_compensated_through_an_inquiry_is_not_sent_again(sent: Sent) -> None:
    handlers.STORE.create(
        context_id=CTX,
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="결제 오류",
        order_id="ord_0001",
        order_no=None,
        first_message="결제가 안 돼요",
        analysis=Analysis(),
    )
    body = {
        "meta": {
            "context_id": CTX,
            "token": "mock:orchestrator",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "request_compensation",
        "input": {
            "inquiry_id": 1,
            "buyer_id": "buy_0001",
            "reason_code": "payment_failure",
            "count": 1,
        },
    }
    first = TestClient(app).post("/a2a", json=body).json()
    assert first["output"]["unit_value"] == 2000  # 문의 경로도 같은 정책표
    out = call(["buy_0001", "buy_0002"])["output"]
    assert [r["status"] for r in out["results"]] == ["duplicate", "requested"]
    assert len(sent) == 2


def test_when_marketing_is_unreachable_the_rest_are_not_sent(sent: Sent) -> None:
    sent.replies.append(reply(TaskStatus.FAILED, outcome="unreachable"))
    result = call(FOUR)
    out = result["output"]
    assert result["outcome"] == "partial" and out["stopped_early"] is True
    assert [r["status"] for r in out["results"]] == ["failed", "skipped", "skipped", "skipped"]
    assert len(sent) == 1
    assert out["requested"] == 0


def test_a_held_coupon_does_not_stop_the_others_and_is_not_a_failure(sent: Sent) -> None:
    sent.replies.append(
        reply(TaskStatus.INPUT_REQUIRED, {"review_id": "rev_1"}, "held", "human_input")
    )
    out = call(FOUR)["output"]
    assert [r["status"] for r in out["results"]] == ["held", "requested", "requested", "requested"]
    assert out["requested"] == 3 and out["stopped_early"] is False
    assert out["results"][0]["total_value"] == 0  # 지급 전이라 합계에 넣지 않는다


def test_a_buyer_marketing_already_linked_is_not_counted_as_a_new_coupon(sent: Sent) -> None:
    # 마케팅은 같은 사건·구매자에게 번호만 달라도 새로 지급하지 않고 `linked_duplicate`로 잇는다
    sent.replies.append(reply(TaskStatus.COMPLETED, {"coupon_id": "cpn_0009"}, "linked_duplicate"))
    out = call(["buy_0001", "buy_0002"])["output"]
    first, second = out["results"]
    assert (first["status"], first["total_value"]) == ("duplicate", 0)
    assert first["marketing_outcome"] == "linked_duplicate" and first["coupon_id"] == "cpn_0009"
    assert second["status"] == "requested" and second["sequence"] == 2  # 번호는 소비됐다
    assert out["requested"] == 1


def test_a_rejected_request_stops_the_rest_instead_of_repeating_the_same_error(
    sent: Sent,
) -> None:
    sent.replies.append(reply(TaskStatus.REJECTED, outcome="SEQUENCE_GAP"))
    result = call(FOUR)
    out = result["output"]
    assert result["outcome"] == "partial" and out["stopped_early"] is True
    assert [r["status"] for r in out["results"]] == ["failed", "skipped", "skipped", "skipped"]
    assert out["results"][0]["marketing_outcome"] == "SEQUENCE_GAP"
    assert len(sent) == 1


def test_a_failed_request_keeps_the_sequence_for_a_retry(sent: Sent) -> None:
    sent.replies.append(reply(TaskStatus.FAILED, outcome="unreachable"))
    call(["buy_0001"])
    retry = call(["buy_0001"])["output"]
    assert retry["results"][0]["status"] == "requested"
    assert retry["results"][0]["sequence"] == sent[0].sequence  # 소비되지 않은 번호로 다시 보낸다


def test_the_output_carries_no_customer_text_or_names(sent: Sent) -> None:
    text = str(call(FOUR))
    assert "김하늘" not in text and "untrusted" not in text.lower().replace('"untrusted": {}', "")
