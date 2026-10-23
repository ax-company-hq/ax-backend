"""hr 연동: list_in_progress·verify_handover skill, decision_rejected 전송, warn 무시 확정 기록.

계약은 #103 (`contracts/src/ax_contracts/schemas/planning.py`·`hr.py`·`record.py`).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import ExecuteDecision
from ax_contracts.schemas.planning import ListInProgressOutput, VerifyHandoverOutput
from ax_contracts.schemas.record import RecordCase, RecordKind
from planning_agent.domain.service import ActionError, PlanningService
from planning_agent.main import app
from planning_agent.runtime import get_service, set_service


def run(coro):
    return asyncio.run(coro)


def _call(body: dict) -> dict:
    return TestClient(app).post("/a2a", json=body).json()


def _list(a2a_body, owner_id: str, area: str | None = None, token: str = "mock:hr") -> dict:
    payload = {"owner_id": owner_id} | ({"area": area} if area else {})
    return _call(a2a_body("list_in_progress", payload, token=token, context_id="ctx_hnd_0001"))


def _verify(a2a_body, ids: list[str], owner_id: str = "emp_01", area: str | None = None) -> dict:
    payload = {"handover_id": "hnd_1", "owner_id": owner_id, "handover_item_ids": ids}
    if area:
        payload["area"] = area
    return _call(
        a2a_body("verify_handover", payload, token="mock:safety", context_id="ctx_hnd_0001")
    )


def _start_spec7(service) -> None:
    """SPEC-7 확정 + PR 생성: T-11(백엔드)·T-12(프론트), PR #31(T-11)·PR #32(T-12)."""
    run(service.answer_question("SPEC-7", "품절 상품도 남겨 두고 품절 표시해 주세요"))
    run(service.confirm_spec("SPEC-7"))
    run(service.generate_prs("SPEC-7"))


def _domain(service) -> dict:
    """활동 로그·창구를 뺀 도메인 상태 (read skill이 바꾸면 안 되는 부분)."""
    snap = service.snapshot()
    return {k: v for k, v in snap.items() if k not in ("activity", "outbox", "seq")}


class CaptureGate:
    """execute 호출을 params까지 기록하는 관문 (모두 executed)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, action, params, meta, evidence) -> ExecuteDecision:
        self.calls.append((action, params))
        return ExecuteDecision(execution_id=f"exe_{len(self.calls)}", grade="A", status="executed")

    def params(self, action: str) -> list[dict]:
        return [p for a, p in self.calls if a == action]


@pytest.fixture
def gate() -> CaptureGate:
    capture = CaptureGate()
    set_service(PlanningService(gate=capture))
    return capture


def _svc() -> PlanningService:
    return get_service()


# ---- list_in_progress


def test_list_in_progress_seed_has_unconfirmed_deployment(service, a2a_body) -> None:
    before = _domain(service)
    result = _list(a2a_body, "emp_01")
    assert result["status"] == "completed"
    out = ListInProgressOutput.model_validate(result["output"])
    assert [(i.item_id, i.kind, i.status, i.area) for i in out.items] == [
        ("dep_pr_28", "deployment", "incident_follow_up_open", "payment")
    ]
    assert out.items[0].owner_id == "emp_01"
    assert out.items[0].updated_at is not None  # 머지 시각
    refs = result["evidence"]["ref_ids"]
    for ref in ("item:dep_pr_28", "pr:28", "task:T-9", "incident:ctx_inc_0001", "req:REQ-101"):
        assert ref in refs
    assert _domain(service) == before  # 상태를 바꾸지 않는다 (활동 로그만)
    last = service.state.activity[-2:]
    assert [(a.from_, a.to, a.point) for a in last] == [
        ("hr", "planning", "list_in_progress"),
        ("planning", "hr", "list_in_progress"),
    ]


def test_list_in_progress_tickets_prs_and_owner_filter(service, a2a_body) -> None:
    _start_spec7(service)
    mine = ListInProgressOutput.model_validate(_list(a2a_body, "emp_01")["output"])
    assert [(i.item_id, i.kind) for i in mine.items] == [
        ("tkt_T-11", "ticket"),
        ("dep_pr_28", "deployment"),
        ("pr_31", "pr"),
    ]
    assert {i.owner_id for i in mine.items} == {"emp_01"}
    assert next(i for i in mine.items if i.item_id == "pr_31").area == "payment"
    # 다른 담당자(프론트 = emp_02) 항목은 빠진다
    other = ListInProgressOutput.model_validate(_list(a2a_body, "emp_02")["output"])
    assert [i.item_id for i in other.items] == ["tkt_T-12", "pr_32"]
    assert not {i.item_id for i in mine.items} & {i.item_id for i in other.items}


def test_list_in_progress_area_filter_and_unknown_owner(service, a2a_body) -> None:
    _start_spec7(service)
    assert len(_list(a2a_body, "emp_01", area="payment")["output"]["items"]) == 3
    none = _list(a2a_body, "emp_01", area="search")
    assert none["status"] == "completed" and none["output"]["items"] == []
    assert none["evidence"]["data_summary"].startswith("확인할 수 없음")
    nobody = _list(a2a_body, "emp_99")
    assert nobody["output"]["items"] == []


def test_list_in_progress_merged_pr_becomes_deployment_until_request_closed(
    service, a2a_body
) -> None:
    _start_spec7(service)
    run(service.merge_pr("PR #31"))  # REQ-12는 T-12가 남아 아직 작업 중
    items = _list(a2a_body, "emp_01")["output"]["items"]
    assert [(i["item_id"], i["status"]) for i in items] == [
        ("dep_pr_28", "incident_follow_up_open"),
        ("dep_pr_31", "merged_unconfirmed"),
    ]
    run(service.review_pr("PR #32"))
    run(service.merge_pr("PR #32"))  # REQ-12 완료 → PR #31 확인 끝
    items = _list(a2a_body, "emp_01")["output"]["items"]
    assert [i["item_id"] for i in items] == ["dep_pr_28"]


def test_list_in_progress_acl_and_invalid_input(a2a_body) -> None:
    denied = _list(a2a_body, "emp_01", token="mock:finance")
    assert (denied["status"], denied["outcome"]) == ("rejected", "FORBIDDEN_SKILL")
    allowed = _list(a2a_body, "emp_01", token="mock:orchestrator")
    assert allowed["status"] == "completed"
    bad = _call(a2a_body("list_in_progress", {"area": "payment"}, token="mock:hr"))
    assert (bad["status"], bad["outcome"]) == ("failed", "invalid_input")


def test_list_in_progress_is_deterministic(service, a2a_body) -> None:
    first = _list(a2a_body, "emp_01")
    second = _list(a2a_body, "emp_01")
    assert first["output"]["items"] == second["output"]["items"]
    assert first["evidence"] == second["evidence"]


# ---- verify_handover


def test_verify_handover_complete(service, a2a_body) -> None:
    before = _domain(service)
    result = _verify(a2a_body, ["dep_pr_28", "card_9304adb5e780"])  # 결정 카드 ID는 비교 안 함
    assert (result["status"], result["outcome"]) == ("completed", None)
    out = VerifyHandoverOutput.model_validate(result["output"])
    assert (out.approved, out.reason_code, out.missing, out.stale_item_ids) == (
        True,
        "complete",
        [],
        [],
    )
    assert "handover:hnd_1" in result["evidence"]["ref_ids"]
    assert _domain(service) == before


def test_verify_handover_one_missing_is_rejected(service, a2a_body) -> None:
    result = _verify(a2a_body, [])
    assert (result["status"], result["outcome"]) == ("completed", "rejected_by_review")
    out = VerifyHandoverOutput.model_validate(result["output"])
    assert (out.approved, out.reason_code) == (False, "missing_items")
    assert [i.item_id for i in out.missing] == ["dep_pr_28"]
    assert "누락 1건" in result["evidence"]["data_summary"]
    reply = service.state.activity[-1]
    assert (reply.from_, reply.to, reply.point) == ("planning", "safety", "verify_handover")
    assert "missing_items" in reply.text


def test_verify_handover_stale_items_are_info_only(service, a2a_body) -> None:
    _start_spec7(service)
    run(service.merge_pr("PR #31"))  # tkt_T-11·pr_31 끝남, dep_pr_31 생김
    listed = ["tkt_T-11", "pr_31", "dep_pr_28", "dep_pr_31", "tkt_T-12", "pr_999"]
    result = _verify(a2a_body, listed)
    out = VerifyHandoverOutput.model_validate(result["output"])
    assert out.approved and out.reason_code == "complete"
    assert out.stale_item_ids == ["tkt_T-11", "pr_31"]  # 다른 담당자의 진행 중 tkt_T-12는 아님
    assert "확인할 수 없음: pr_999" in result["evidence"]["data_summary"]

    # stale이 있어도 누락이 있으면 거절 (stale은 판정에 쓰지 않는다)
    result = _verify(a2a_body, ["tkt_T-11", "pr_31", "dep_pr_28"])
    out = VerifyHandoverOutput.model_validate(result["output"])
    assert (out.approved, [i.item_id for i in out.missing]) == (False, ["dep_pr_31"])
    assert out.stale_item_ids == ["tkt_T-11", "pr_31"]


def test_verify_handover_other_owner_and_area(service, a2a_body) -> None:
    _start_spec7(service)
    # emp_02의 항목은 emp_01 인계 판정에 끼지 않는다
    ok = _verify(a2a_body, ["tkt_T-11", "pr_31", "dep_pr_28"])
    assert ok["output"]["approved"] is True
    # 영역이 다르면 진행 중 항목이 없다 → 빈 목록도 승인
    other_area = _verify(a2a_body, [], area="search")
    assert other_area["output"]["approved"] is True


def test_verify_handover_acl_and_invalid_input(a2a_body) -> None:
    payload = {"handover_id": "hnd_1", "owner_id": "emp_01"}
    denied = _call(a2a_body("verify_handover", payload, token="mock:hr"))
    assert (denied["status"], denied["outcome"]) == ("rejected", "FORBIDDEN_SKILL")
    bad = _call(a2a_body("verify_handover", {"owner_id": "emp_01"}, token="mock:safety"))
    assert (bad["status"], bad["outcome"]) == ("failed", "invalid_input")


# ---- decision_rejected


def _rejected_cases(gate: CaptureGate) -> list[RecordCase]:
    cases = [RecordCase.model_validate(p["record_case"]) for p in gate.params("record_case")]
    return [c for c in cases if c.kind == RecordKind.DECISION_REJECTED]


def test_reject_draft_sends_decision_rejected(gate) -> None:
    svc = _svc()
    result = run(
        svc.reject_draft(
            "REQ-13",
            decision_reason="안내 문구만 바꿔서는 반복 질문이 줄지 않는다",
            alternatives_considered=["운영 매뉴얼에 FAQ 추가", "결제 실패 화면 개편"],
        )
    )
    req = next(r for r in svc.state.requests if r.id == "REQ-13")
    assert req.state == "기각"
    assert result.output["record_kind"] == "decision_rejected"
    [case] = _rejected_cases(gate)
    assert case.context_id == "ctx_req_0013"
    assert case.decision_reason == "안내 문구만 바꿔서는 반복 질문이 줄지 않는다"
    assert case.alternatives_considered == ["운영 매뉴얼에 FAQ 추가", "결제 실패 화면 개편"]
    assert case.author == "planning" and "req:REQ-13" in case.source_refs
    keys = [c.idempotency_key for c in svc.gate_calls if c.action == "record_case"]
    assert keys == ["ctx_req_0013:record_case:REQ-13:decision_rejected"]
    decision = svc.state.decisions[-1]
    assert decision.text.startswith("[기각한 안]") and decision.sent_to_hr
    to_hr = [a for a in svc.state.activity if a.to == "hr" and a.point == "record_case"]
    assert "decision_rejected" in to_hr[-1].text
    assert any(m.to == "hr" and "decision_rejected" in m.text for m in svc.state.outbox)


@pytest.mark.parametrize(
    ("reason", "alternatives"),
    [("", ["대안"]), ("이유", []), ("   ", ["  "])],
)
def test_reject_without_reason_or_alternatives_is_not_sent(gate, reason, alternatives) -> None:
    svc = _svc()
    before = svc.snapshot()
    with pytest.raises(ActionError) as err:
        run(
            svc.reject_draft("REQ-13", decision_reason=reason, alternatives_considered=alternatives)
        )
    assert err.value.code == "MISSING_DECISION_REASON"
    with pytest.raises(ActionError):
        run(svc.reject_decision("SPEC-7", "재시도 버튼만 추가", reason, alternatives))
    assert svc.snapshot() == before
    assert gate.params("record_case") == []  # hr에 보내지 않았다


def test_reject_decision_records_rejected_alternative(gate) -> None:
    svc = _svc()
    result = run(
        svc.reject_decision(
            "SPEC-7",
            "재시도 버튼만 추가",
            decision_reason="장바구니가 비면 재시도해도 다시 담아야 한다",
            alternatives_considered=["장바구니 보존과 함께 재시도 버튼"],
        )
    )
    [case] = _rejected_cases(gate)
    assert case.context_id == "ctx_req_0012" and case.summary == "[기각한 안] 재시도 버튼만 추가"
    assert result.output["decision_id"] == svc.state.decisions[-1].id
    assert svc.state.decisions[-1].spec == "SPEC-7"


def test_rest_reject_endpoint(service) -> None:
    client = TestClient(app)
    res = client.post("/api/planning/requests/REQ-14/reject", json={"decision_reason": "x"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "MISSING_DECISION_REASON"
    assert next(r for r in service.state.requests if r.id == "REQ-14").state == "접수"
    res = client.post(
        "/api/planning/requests/REQ-14/reject",
        json={"decision_reason": "일시적 지표 흔들림", "alternatives_considered": ["관찰 1주"]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["changed"] is True
    assert body["meta"]["result"]["record_kind"] == "decision_rejected"
    assert next(r for r in body["data"]["requests"] if r["id"] == "REQ-14")["state"] == "기각"
    missing = client.post(
        "/api/planning/requests/REQ-999/reject",
        json={"decision_reason": "a", "alternatives_considered": ["b"]},
    )
    assert missing.status_code == 404
    # /actions의 camelCase도 같은 길
    res = client.post("/api/planning/actions", json={"type": "rejectDraft", "request": "REQ-13"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "MISSING_DECISION_REASON"


# ---- warn 무시 확정 (FR-SPEC-006)


def test_confirm_with_warn_records_warn_ignored(gate) -> None:
    svc = _svc()
    run(svc.confirm_spec("SPEC-7"))  # R2에 인사 경고(DC-31)가 남아 있다
    flow = [a for a in svc.state.activity if a.context_id == "ctx_req_0012"]
    audit = [a for a in flow if a.point == "audit.warn_ignored"]
    assert len(audit) == 1 and "warn 무시 진행" in audit[0].text and "R2(DC-31)" in audit[0].text
    assert any(a.to == "hr" and "warn 무시 진행" in a.text for a in flow)
    [confirm] = gate.params("confirm_spec")
    assert confirm["warn_ignored"][0] == {
        "para": "R2",
        "card": "DC-31",
        "text": '"재시도 버튼만 추가"는 9/30 기각된 안. R1과 함께일 때만 진행',
    }
    [case_params] = gate.params("record_case")
    case = RecordCase.model_validate(case_params["record_case"])
    assert case.kind == RecordKind.SPEC_CONFIRMED
    assert "warn 무시 진행" in case.summary
    assert {"para:SPEC-7:R2", "card:DC-31"} <= set(case.source_refs)
    assert case_params["warn_ignored"] == confirm["warn_ignored"]


def test_confirm_without_warn_has_no_warn_ignored(gate) -> None:
    svc = _svc()
    for p in next(s for s in svc.state.specs if s.id == "SPEC-7").paras:
        p.warn = None
    run(svc.confirm_spec("SPEC-7"))
    assert not [a for a in svc.state.activity if a.point == "audit.warn_ignored"]
    assert "warn_ignored" not in gate.params("confirm_spec")[0]
    [case_params] = gate.params("record_case")
    assert "warn_ignored" not in case_params
    assert "warn 무시" not in RecordCase.model_validate(case_params["record_case"]).summary
