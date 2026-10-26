"""변경 요청 결과 통지 (`receive_change_result`, 계약 #293): 요청을 닫을 때 요청 부서에 보낸다."""

from __future__ import annotations

import asyncio

from ax_contracts.schemas.planning import ChangeResultInput
from planning_agent.result_notify import RECEIVERS, idempotency_key, send_change_result


def run(coro):
    return asyncio.run(coro)


def _capture(service) -> list[tuple[str, str, ChangeResultInput]]:
    sent: list[tuple[str, str, ChangeResultInput]] = []

    async def fake(dept, ctx, tenant, result):
        sent.append((dept, ctx, result))
        return "보냄 (테스트)"

    service._send_result = fake
    return sent


def test_simple_request_completion_notifies_requester(service) -> None:
    sent = _capture(service)
    run(service.inbound("support_simple"))
    req = next(r for r in service.state.requests if r.id == "REQ-21")
    assert req.state == "완료"
    [(dept, ctx, result)] = sent
    assert (dept, ctx) == ("support", req.context_id)
    assert result.status == "completed" and result.request_id == "REQ-21"
    assert result.pr_numbers and result.merged_at is not None and result.reason_code is None
    acts = [a for a in service.state.activity if a.point == "receive_change_result"]
    assert acts and acts[-1].to == "support" and "보냄" in acts[-1].text


def test_draft_request_merge_notifies_with_spec(service) -> None:
    # 개선 초안(hr)은 자동 채택 → 기획서 → PR → 머지로 닫히고, hr에 결과가 간다
    sent = _capture(service)
    run(service.inbound("hr_draft"))
    req = next(r for r in service.state.requests if r.id == "REQ-21")
    assert req.state == "완료"
    [(dept, _, result)] = [x for x in sent if x[2].request_id == "REQ-21"]
    assert dept == "hr" and result.spec_id == req.spec and result.status == "completed"


def test_rejected_draft_notifies_declined(service) -> None:
    sent = _capture(service)
    service.state.requests.append(
        next(r for r in service.state.requests if r.id == "REQ-13").model_copy(
            update={"id": "REQ-90", "state": "접수", "spec": None}
        )
    )
    run(
        service.reject_draft(
            "REQ-90",
            decision_reason="같은 질문이 줄지 않는다",
            alternatives_considered=["FAQ 추가"],
        )
    )
    declined = [res for _, _, res in sent if res.request_id == "REQ-90"]
    assert declined and declined[0].status == "declined"
    assert declined[0].reason_code == "draft_rejected" and declined[0].pr_numbers == []


def test_sender_failure_does_not_block_closing(service) -> None:
    async def broken(*_):
        raise RuntimeError("down")

    service._send_result = broken
    run(service.inbound("support_simple"))
    req = next(r for r in service.state.requests if r.id == "REQ-21")
    assert req.state == "완료"
    assert any(
        "보내지 못함" in a.text
        for a in service.state.activity
        if a.point == "receive_change_result"
    )


def test_default_sender_skips_network_in_mock_and_non_receivers() -> None:
    result = ChangeResultInput(request_id="REQ-1", status="declined", summary="REQ-1 기각")
    assert run(send_change_result("marketing", "ctx_req_0001", "shop-demo", result)).startswith(
        "기록만"
    )
    assert (
        run(send_change_result("support", "ctx_req_0001", "shop-demo", result))
        == "기록만 (목 모드)"
    )
    assert RECEIVERS == {"support", "hr", "data"}
    assert idempotency_key("ctx_req_0001", "REQ-1") == "ctx_req_0001:change_result:REQ-1"
