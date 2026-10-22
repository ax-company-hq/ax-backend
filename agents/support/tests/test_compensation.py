from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, RequestMeta, TaskStatus
from support_agent import compensation, handlers
from support_agent.main import app, load_card
from support_agent.schemas import Analysis
from support_agent.store import InquiryStore


class Sent(list):
    """마케팅으로 보낸 요청 목록. `replies`에 넣은 결과를 차례로 돌려준다."""

    def __init__(self) -> None:
        super().__init__()
        self.replies: list[AgentResult] = []


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> "Sent":
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    store = InquiryStore()
    store.create(
        context_id="ctx_inq_0001",
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="배송 지연",
        order_id=101,
        order_no="ORD-1",
        first_message="배송이 늦어요",
        analysis=Analysis(),
    )
    monkeypatch.setattr(handlers, "STORE", store)
    monkeypatch.setattr(handlers, "LEDGER", compensation.SequenceLedger())
    sent = Sent()
    replies = sent.replies

    async def fake_send(target: str, req: AgentRequest) -> AgentResult:
        assert target == "marketing"
        sent.append(req)
        if replies:
            return replies.pop(0)
        return AgentResult(
            meta=req.meta,
            status=TaskStatus.COMPLETED,
            output={"coupon_id": "cpn_0001", "count": req.input["count"]},
        )

    monkeypatch.setattr(handlers, "SEND", fake_send)
    return sent


def call(
    inquiry_id: int = 1, reason: str = "late_delivery", count: int = 1, key: str | None = None
):
    body = {
        "meta": {
            "context_id": "ctx_inq_0001",
            "token": "mock:orchestrator",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": "request_compensation",
        "input": {
            "inquiry_id": inquiry_id,
            "buyer_id": "usr_1",
            "reason_code": reason,
            "count": count,
        },
    }
    return TestClient(app).post("/a2a", json=body).json()


def test_policy_decides_unit_and_total_in_code() -> None:
    decision = compensation.decide("damaged", 2)
    assert (decision.unit_value, decision.total_value) == (3000, 6000)
    assert not compensation.decide("changed_mind", 1).eligible
    assert not compensation.decide("damaged", compensation.MAX_COUNT + 1).eligible


def test_card_declares_skill() -> None:
    card = load_card()
    assert "request_compensation" in {s.id for s in card.skills}


def test_requests_marketing_with_sequence_and_computed_total(_fresh: Sent) -> None:
    result = call(reason="late_delivery", count=2, key="k-1")
    assert result["status"] == "completed"
    request = _fresh[0]
    assert request.skill == "issue_coupon"
    assert request.sequence == 1
    assert request.input == {
        "purpose_code": "compensation",
        "buyer_id": "usr_1",
        "count": 2,
        "unit_value": 2000,
        "claimed_total": 4000,
    }
    assert result["output"]["total_value"] == 4000
    assert "4000" in result["evidence"]["data_summary"]


def test_sequence_increments_per_context(_fresh: Sent) -> None:
    call(reason="late_delivery", key="k-1")
    call(reason="damaged", key="k-2")
    assert [r.sequence for r in _fresh] == [1, 2]


def test_same_key_returns_previous_result_without_resending(_fresh: Sent) -> None:
    first = call(key="k-1")
    second = call(key="k-1")
    assert len(_fresh) == 1
    assert second["output"] == first["output"]


def test_not_eligible_reason_does_not_call_marketing(_fresh: Sent) -> None:
    result = call(reason="changed_mind", key="k-1")
    assert result["outcome"] == "not_eligible"
    assert result["output"]["eligible"] is False
    assert _fresh == []


def test_unknown_inquiry_is_not_found(_fresh: Sent) -> None:
    assert call(inquiry_id=999)["outcome"] == "not_found"
    assert _fresh == []


def test_held_reply_consumes_sequence_but_is_not_cached(_fresh: Sent) -> None:
    held = AgentResult(
        meta=_meta(),
        status=TaskStatus.INPUT_REQUIRED,
        reason_code="human_input",
        outcome="held",
        output={"execution_id": "exe_1", "review_id": "rev_1"},
    )
    _fresh.replies.append(held)
    first = call(key="k-1")
    assert first["output"]["marketing_status"] == "input-required"
    second = call(key="k-1")  # 승인 후 같은 키 재요청: 다시 보낸다
    assert [r.sequence for r in _fresh] == [1, 2]
    assert second["output"]["marketing_status"] == "completed"


def test_verifying_reply_does_not_consume_sequence(_fresh: Sent) -> None:
    verifying = AgentResult(
        meta=_meta(),
        status=TaskStatus.WORKING,
        outcome="verifying",
        output={"execution_id": "exe_1", "grade": "B"},
    )
    _fresh.replies.append(verifying)
    call(key="k-1")
    call(key="k-1")
    assert [r.sequence for r in _fresh] == [1, 1]


def test_sequence_gap_rejection_does_not_consume_sequence(_fresh: Sent) -> None:
    _fresh.replies.append(
        AgentResult(meta=_meta(), status=TaskStatus.REJECTED, outcome="SEQUENCE_GAP", output={})
    )
    call(key="k-1")
    call(key="k-1")
    assert [r.sequence for r in _fresh] == [1, 1]


def test_marketing_timeout_with_execution_info_consumes_sequence(_fresh: Sent) -> None:
    _fresh.replies.append(
        AgentResult(
            meta=_meta(),
            status=TaskStatus.CANCELED,
            outcome="TIMEOUT",
            output={"execution_id": "exe_1", "grade": "B"},
        )
    )
    call(key="k-1")
    call(key="k-1")
    assert [r.sequence for r in _fresh] == [1, 2]


@pytest.mark.asyncio
async def test_concurrent_requests_in_same_context_get_distinct_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(handlers, "LEDGER", compensation.SequenceLedger())
    seen: list[int | None] = []

    async def slow_send(target: str, req: AgentRequest) -> AgentResult:
        seen.append(req.sequence)
        await asyncio.sleep(0.01)
        return AgentResult(meta=req.meta, status=TaskStatus.COMPLETED, output={})

    monkeypatch.setattr(handlers, "SEND", slow_send)

    def req(buyer: str) -> AgentRequest:
        return AgentRequest(
            meta=_meta().model_copy(update={"idempotency_key": f"k-{buyer}"}),
            skill="request_compensation",
            input={"inquiry_id": 1, "buyer_id": buyer, "reason_code": "damaged", "count": 1},
        )

    await asyncio.gather(
        handlers.request_compensation(req("a")), handlers.request_compensation(req("b"))
    )
    assert sorted(s for s in seen if s) == [1, 2]


def test_timeout_keeps_sequence_for_retry(_fresh: Sent) -> None:
    timeout = AgentResult(meta=_meta(), status=TaskStatus.CANCELED, outcome="TIMEOUT", output={})
    _fresh.replies.append(timeout)
    call(key="k-1")
    call(key="k-1")
    assert [r.sequence for r in _fresh] == [1, 1]


def _meta():
    return RequestMeta(
        context_id="ctx_inq_0001",
        token="mock:marketing",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
