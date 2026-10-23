"""서비스 시나리오: 프로토타입 리듀서와 같은 결과가 나오는지."""

from __future__ import annotations

import asyncio

from ax_agent_template import execute as gate_module
from ax_contracts.schemas import ExecuteDecision
from planning_agent.domain.models import Doc
from planning_agent.domain.projects import gather, project_summary, stage_done
from planning_agent.domain.rules import check_simple
from planning_agent.domain.trace import trace_of, trace_text


def run(coro):
    return asyncio.run(coro)


def req(service, req_id: str):
    return next(r for r in service.state.requests if r.id == req_id)


def acts(service, ctx: str):
    return [a for a in service.state.activity if a.context_id == ctx]


# ---- 중복 연결


def test_same_title_twice_becomes_one_request_and_linked_duplicate(service) -> None:
    results = [run(service.inbound("support_ticket")) for _ in range(4)]
    coupon = [r for r in service.state.requests if r.title.startswith("쿠폰 적용 후")]
    assert len(coupon) == 2
    original, dup = coupon
    assert (original.id, original.duplicate_of) == ("REQ-21", None)
    assert original.state != "접수" and original.spec is not None  # 접수 뒤 기획서부터 자동 진행
    assert (dup.id, dup.state, dup.duplicate_of) == ("REQ-24", "중복 연결", "REQ-21")
    assert results[3].outcome == "linked_duplicate"
    # 2번째는 기존 REQ-12와 같은 제목 → 연결
    assert req(service, "REQ-22").duplicate_of == "REQ-12"
    assert results[1].outcome == "linked_duplicate"
    assert "linked_duplicate" in acts(service, dup.context_id)[0].text
    assert len([s for s in service.state.specs if s.request == "REQ-24"]) == 0


# ---- 간단 요청 무인 처리


def test_simple_request_completes_without_human(service) -> None:
    result = run(service.inbound("support_simple"))
    r = req(service, "REQ-21")
    assert r.title == "상품 상세의 배송 안내 문구 오타 수정"
    assert r.auto and r.auto.ok and r.state == "완료"
    assert result.output == {"request_id": "REQ-21", "auto": True}
    spec = next(s for s in service.state.specs if s.id == r.spec)
    assert (spec.id, spec.status, spec.auto) == ("SPEC-8", "확정", True)
    pr = service.state.prs[-1]
    assert (pr.id, pr.merged, pr.auto, pr.gate.paths) == ("PR #31", True, True, "shop-web/")
    assert pr.merged_at is not None
    assert pr.changed_paths == ["shop-web/pages/notice.tsx"]
    steps = acts(service, r.context_id)
    assert len(steps) >= 10
    points = [a.point for a in steps]
    assert "execute(merge_pr)" in points and "github_merge_pull" in points
    assert next(a for a in steps if a.point == "execute(merge_pr)").grade == "B"
    assert next(a for a in steps if a.point == "execute(generate_pr)").grade == "B"
    assert not any(a.kind == "사람" for a in steps)  # 사람 클릭 0번
    assert steps[-1].to == "support" and "완료" in steps[-1].text
    assert {m.to for m in service.state.outbox} >= {"hr", "support", "safety", "shop"}
    assert service.state.first_pr_minutes[-1] == 3
    assert service.state.auto_policy.history[-1].pr == "PR #31"


def test_three_simple_requests_propose_then_approve_promotion(service) -> None:
    for _ in range(2):
        run(service.inbound("support_simple"))
    assert service.state.auto_policy.proposal is None
    run(service.inbound("support_simple"))
    pol = service.state.auto_policy
    assert pol.level == 1 and pol.streak == 3
    assert pol.proposal is not None and pol.proposal.to == 2
    assert pol.log[-1].by == "자동"
    propose = [a for a in service.state.activity if a.point == "auto_policy.propose"]
    assert len(propose) == 1 and propose[0].grade == "C" and propose[0].kind == "execute"
    assert "auto_policy_propose" in [c.action for c in service.gate_calls]

    run(service.approve_promotion())
    pol = service.state.auto_policy
    assert (pol.level, pol.streak, pol.proposal) == (2, 0, None)
    assert pol.log[-1].by == "사람" and "L1 → L2" in pol.log[-1].text
    assert service.state.activity[-1].point == "auto_policy.updated"


def test_promotion_proposal_held_in_c_queue_is_normal(service, monkeypatch) -> None:
    async def hold_c(request) -> ExecuteDecision:
        if request.action == "auto_policy_propose":
            return ExecuteDecision(
                execution_id="exe_hold",
                grade="C",
                status="held",
                hold_reason="over_threshold",
                review_id="rev_1",
            )
        return ExecuteDecision(execution_id="exe_ok", grade="A", status="executed")

    monkeypatch.setattr(gate_module, "_send_to_safety", hold_c)
    for _ in range(3):
        run(service.inbound("support_simple"))
    assert service.state.auto_policy.proposal is not None
    assert service.state.auto_policy.level == 1  # 승인 전에는 그대로


def test_reject_promotion_resets_streak(service) -> None:
    for _ in range(3):
        run(service.inbound("support_simple"))
    run(service.reject_promotion())
    pol = service.state.auto_policy
    assert (pol.level, pol.streak, pol.proposal) == (1, 0, None)
    assert "승급 보류" in pol.log[-1].text


def test_auto_pr_incident_demotes_scope(service) -> None:
    for _ in range(3):
        run(service.inbound("support_simple"))
    run(service.approve_promotion())
    assert service.state.auto_policy.level == 2
    result = run(service.inbound("orch_incident"))
    pol = service.state.auto_policy
    assert pol.level == 1 and pol.streak == 0 and pol.proposal is None
    last_pr = service.state.prs[-1].id
    assert next(h for h in pol.history if h.pr == last_pr).result == "장애 연결"
    assert pol.log[-1].by == "자동" and "L2 → L1" in pol.log[-1].text
    assert result.output["incident"] == "ctx_inc_0002"
    assert service.state.incidents[0].context_id == "ctx_inc_0002"
    follow = req(service, result.output["follow_up_request_id"])
    assert follow.title == f"{last_pr} 관련 재발 방지 검토"
    assert any(a.point == "auto_policy.updated" for a in acts(service, "ctx_inc_0002"))


def test_whole_text_request_waits_for_human_at_l1(service) -> None:
    for _ in range(3):
        run(service.inbound("marketing_simple"))
    r = next(x for x in service.state.requests if x.title == "기획전 페이지 안내 문구 전체 교체")
    # L1 기준을 넘으면 간단 요청 무인 처리(_auto_complete)는 안 하고, 일반 흐름(기획서 초안 →
    # 확정 규칙 → PR → 머지 규칙)으로 간다. 사람이 누를 차례는 규칙에 걸릴 때만이다
    assert r.auto is not None and not r.auto.ok
    assert any("L1 기준(30줄·2파일) 초과" in reason for reason in r.auto.reasons)
    assert r.flow and r.spec is not None and r.state != "접수"
    points = [(a.point, a.text) for a in acts(service, r.context_id)]
    assert ("get_request_status", f"{r.id} 접수됨 → 기획서 작성") in points
    assert any(p == "write_spec" for p, _ in points)
    # 같은 제목도 L2 기준이면 크기는 통과한다
    pol = service.state.auto_policy.model_copy(update={"level": 2})
    assert check_simple(r.title, pol).ok


def test_l0_stops_auto_processing(service) -> None:
    service.state.auto_policy.level = 0
    run(service.inbound("support_simple"))
    r = req(service, "REQ-21")
    assert r.state == "접수" and not r.auto.ok
    assert "L0" in r.auto.reasons[0]


# ---- 대표 흐름 REQ-12


def test_req12_main_flow(service) -> None:
    run(service.answer_question("SPEC-7", "품절 상품도 남겨 두고 품절 표시해 주세요"))
    spec = next(s for s in service.state.specs if s.id == "SPEC-7")
    assert spec.question is None
    assert spec.paras[0].text == "결제 실패 시 장바구니 상태 보존 (품절 상품은 남기고 품절 표시)"
    decision = service.state.decisions[-1]
    assert (decision.id, decision.spec, decision.from_) == ("D-1", "SPEC-7", "스레드 TH-7")

    # 질문은 닫혔지만 R2 인사 경고(DC-31)가 남아 자동 확정하지 않는다 → 교차검증 대기
    spec = next(s for s in service.state.specs if s.id == "SPEC-7")
    assert spec.status == "초안" and spec.auto_review is not None
    assert (spec.auto_review.state, spec.auto_review.rules) == (
        "교차검증 대기",
        ["spec_rejected_warning"],
    )

    run(service.confirm_spec("SPEC-7"))  # 판단은 기존 사람 확정 버튼
    assert spec.status == "초안"  # 바뀐 것은 서비스 상태 (복사본 교체)
    spec = next(s for s in service.state.specs if s.id == "SPEC-7")
    assert spec.status == "확정"
    tasks = [t for t in service.state.tasks if t.spec == "SPEC-7"]
    assert [(t.id, t.para, t.owner, t.state) for t in tasks] == [
        ("T-11", "R1", "백엔드", "진행 중"),
        ("T-12", "R2", "프론트", "진행 중"),
    ]
    assert tasks[0].title == "결제 실패 시 장바구니 상태 보존"
    assert req(service, "REQ-12").state == "작업 중"

    run(service.generate_prs("SPEC-7"))
    prs = [p for p in service.state.prs if p.task in {"T-11", "T-12"}]
    assert [(p.id, p.reviews) for p in prs] == [("PR #31", ["통과"]), ("PR #32", ["불일치"])]
    # PR #31: 검수 통과 + merge_pr 규칙 통과 → 자동 머지. PR #32: 검수 불일치 → 수정 대기
    assert [t.state for t in service.state.tasks if t.spec == "SPEC-7"] == ["완료", "PR 검수"]
    assert prs[0].changed_paths == ["shop-api/cart/service.py"]
    assert (prs[0].merged, prs[0].auto, prs[0].auto_review) == (True, True, None)
    assert req(service, "REQ-12").state == "작업 중"

    run(service.review_pr("PR #32"))
    pr32 = service.state.prs[-1]
    assert pr32.reviews == ["불일치", "통과"]
    # 재검수 통과지만 결제 화면(shop-web/checkout/)은 머지 위험 경로 → 자동 머지 안 함, 사람 확인
    assert not pr32.merged and pr32.auto_review is not None
    assert pr32.auto_review.state == "사람 확인"
    assert pr32.auto_review.rules == ["irreversible"]

    run(service.merge_pr("PR #31"))  # 이미 자동 머지 → 변화 없음
    assert req(service, "REQ-12").state == "작업 중"
    run(service.merge_pr("PR #32"))  # 판단은 기존 사람 머지 버튼
    assert req(service, "REQ-12").state == "완료"
    assert all(p.merged and p.merged_at for p in service.state.prs)

    flow = acts(service, "ctx_req_0012")
    texts = [a.text for a in flow if a.point == "record_case"]
    assert "① kind spec_confirmed · SPEC-7" in texts
    assert "① kind pr_merged · PR #31 (자동 머지)" in texts
    assert "① kind pr_merged · PR #32" in texts
    assert "① kind request_closed · REQ-12" in texts
    assert flow[-1].to == "support" and "완료" in flow[-1].text
    human = [a for a in flow if a.kind == "사람"]
    assert len(human) == 2  # 확정 1 + 머지 1 (PR #31은 자동 머지)
    assert [a.point for a in flow if a.point == "audit.auto_confirmed"] == []
    assert next(s for s in service.state.specs if s.id == "SPEC-7").auto_review is None
    assert service.state.prs[-1].auto_review is None
    gated = [c.action for c in service.gate_calls]
    for action in ("confirm_spec", "split_tasks", "generate_pr", "merge_pr", "record_case"):
        assert action in gated
    assert service.state.first_pr_minutes == [38, 52]


def test_accept_draft_counts_adoption(service) -> None:
    run(service.accept_draft("REQ-13"))
    assert req(service, "REQ-13").state == "기획 중"
    assert service.state.drafts_accepted == 1
    run(service.accept_draft("REQ-13"))  # 이미 채택 → 변화 없음
    assert service.state.drafts_accepted == 1


# ---- 역추적·프로젝트·문서


def test_trace_of_pr28_chain(service) -> None:
    steps = trace_of(service.state, "PR #28")
    assert [s.kind for s in steps] == [
        "코드",
        "PR",
        "작업",
        "문단",
        "장애",
        "결정",
        "기획서",
        "요청",
        "근거",
    ]
    assert steps[0].title == "shop-api/payment/config.py:12"
    assert steps[0].detail == "PAYMENT_TIMEOUT_MS = 8000"
    assert steps[1].detail == "gen/t-9-payment-timeout · 사람 머지 · 검수 통과"
    assert steps[3].title == "SPEC-5 R3"
    assert steps[4].title == "ctx_inc_0001"
    assert steps[7].title == "REQ-9 느린 카드사에서 결제가 실패한다"
    text = trace_text(steps)
    assert text.startswith("shop-api/payment/config.py:12의 `PAYMENT_TIMEOUT_MS = 8000`는 PR #28")
    assert "SPEC-5 R3" in text and "결제 실패 코드 집계 (데이터분석)" in text


def test_project_req9_six_stages(service) -> None:
    r = req(service, "REQ-9")
    g = gather(service.state, r)
    done = stage_done(r, g)
    assert done == {
        "요청": True,
        "작업": True,
        "운영": True,
        "처리": True,
        "소통": True,
        "문서": False,
    }
    points = {a.point for a in g["activity"]}
    assert {"find_related_prs", "verify_rollback_target"} <= points  # 연결된 사건 ctx_inc_0001
    assert [i.context_id for i in g["incidents"]] == ["ctx_inc_0001"]
    assert [x.id for x in g["follow"]] == ["REQ-101"]
    run(service.gather_doc("REQ-9"))
    summary = project_summary(service.state, req(service, "REQ-9"))
    assert [s["done"] for s in summary["stages"]] == [True] * 6


def test_gather_doc_block_counts(service) -> None:
    result = run(service.gather_doc("REQ-12"))
    assert result.output == {"doc_id": "DOC-2", "blocks": 15}
    doc: Doc = service.state.docs[-1]
    assert (doc.about, doc.by, doc.title) == (
        "REQ-12",
        "human",
        "REQ-12 관련 정리 · 결제 실패 뒤 장바구니가 비워진다",
    )
    assert [b.kind for b in doc.blocks][:3] == ["h2", "ref", "p"]
    assert doc.blocks[-1].text == "고객지원에 결과 회신" and doc.blocks[-1].done is False

    result = run(service.gather_doc("REQ-9", by="ai"))
    assert result.output == {"doc_id": "DOC-3", "blocks": 17}
    assert service.state.docs[-1].blocks[-1].done is True
    last = service.state.activity[-1]
    assert (last.from_, last.kind, last.point) == ("planning", "A2A", "gather_doc")
    assert last.text.startswith("AI 어시스턴트 요청으로 REQ-9")


def test_doc_editing_actions(service) -> None:
    run(service.new_doc("회의 메모"))
    run(service.add_block("DOC-2", "todo", "할 일"))
    run(service.edit_block("DOC-2", "b2", "고친 할 일"))
    run(service.toggle_todo("DOC-2", "b2"))
    run(service.rename_doc("DOC-2", "10/23 메모"))
    doc = service.state.docs[-1]
    assert (doc.title, [(b.id, b.kind, b.text, b.done) for b in doc.blocks]) == (
        "10/23 메모",
        [("b1", "p", "", None), ("b2", "todo", "고친 할 일", True)],
    )
    run(service.remove_block("DOC-2", "b1"))
    assert [b.id for b in service.state.docs[-1].blocks] == ["b2"]


# ---- 소통·연동 창구


def test_messages_decisions_and_outbox(service) -> None:
    run(service.post_message("CH-support", "REQ-12 오늘 확정 예정입니다"))
    out = service.state.outbox[-1]
    assert (out.to, out.point, out.read) == ("support", "message", False)
    assert service.state.seq == 1
    run(service.post_message("CH-team", "팀 공지"))  # 팀 채널은 다른 부서로 나가지 않는다
    assert len(service.state.outbox) == 1
    run(service.mark_read("support"))
    assert service.state.outbox[-1].read is True

    run(service.dept_chat("data"))
    th = next(t for t in service.state.threads if t.id == "CH-data")
    assert th.messages[-1].text.startswith("결제 실패율이 14:00~14:10에 또 올랐습니다")
    run(service.extract_decision("CH-data", 0))
    d = service.state.decisions[-1]
    assert (d.id, d.spec, d.from_) == ("D-1", "-", "소통 데이터분석")
    assert th.messages[0].decision is None  # 위 th는 이전 상태
    th = next(t for t in service.state.threads if t.id == "CH-data")
    assert th.messages[0].decision == "D-1"
    run(service.extract_decision("CH-data", 0))  # 이미 추출 → 변화 없음
    assert len(service.state.decisions) == 2


def test_queries_and_safety_verify_simulator(service) -> None:
    run(service.inbound("infra_query"))
    reply = service.state.activity[-1]
    assert (reply.from_, reply.to, reply.context_id) == ("planning", "infra", "ctx_inc_0001")
    assert "PR #28" in reply.text and "원인 판정은 하지 않음" in reply.text
    result = run(service.inbound("safety_verify"))
    assert result.output == {"approved": True}
    assert "deploy_before_incident" in service.state.activity[-1].text


def test_reset_restores_seed(service) -> None:
    run(service.inbound("support_simple"))
    service.reset()
    assert len(service.state.requests) == 6 and service.state.seq == 0
    assert service.gate_calls == []


# ---- 사람 클릭 없이 다음 단계로 (flow 요청)


def test_general_request_advances_without_clicks(service) -> None:
    run(service.inbound("support_ticket"))
    r = req(service, "REQ-21")
    assert r.flow and r.spec is not None
    sp = next(x for x in service.state.specs if x.id == r.spec)
    assert sp.status == "확정" and sp.auto
    tasks = [t for t in service.state.tasks if t.spec == sp.id]
    prs = [p for p in service.state.prs if p.task in {t.id for t in tasks}]
    assert tasks and len(prs) == len(tasks)  # PR 만들기 버튼 없이 작업마다 PR
    # 각 PR은 자동 머지됐거나, 규칙에 걸려 대기열에 있다 (그때만 사람·교차검증 차례)
    assert all(p.merged or p.auto_review is not None for p in prs)
    points = [a.point for a in acts(service, r.context_id)]
    assert points.index("write_spec") < points.index("split_tasks") < points.index("review_pr")


def test_draft_is_adopted_and_advances(service) -> None:
    run(service.inbound("hr_draft"))
    r = req(service, "REQ-21")
    assert r.flow and r.spec is not None and service.state.drafts_accepted == 1
    assert any(a.point == "review_ticket_draft" for a in acts(service, r.context_id))


def test_mismatch_is_reviewed_again_once(service) -> None:
    # 시드 SPEC-7(요구사항 2개)을 flow 요청으로 두고 사람이 확정하면, PR 만들기부터는 자동이다.
    # 두 번째 PR은 첫 검수가 불일치 → 수정 후 재검수까지 버튼 없이 간다
    req(service, "REQ-12").flow = True
    run(service.confirm_spec("SPEC-7"))
    tasks = {t.id for t in service.state.tasks if t.spec == "SPEC-7"}
    prs = [p for p in service.state.prs if p.task in tasks]
    assert len(prs) == 2
    assert [p.reviews for p in prs] == [["통과"], ["불일치", "통과"]]


def test_l0_keeps_general_request_at_intake(service) -> None:
    service.state.auto_policy.level = 0
    run(service.inbound("support_ticket"))
    r = req(service, "REQ-21")
    assert (r.state, r.spec) == ("접수", None)


def test_seed_requests_are_not_advanced(service) -> None:
    before = {x.id: (x.state, x.spec) for x in service.state.requests}
    run(service.inbound("data_query"))
    assert {x.id: (x.state, x.spec) for x in service.state.requests} == before


# ---- 총괄 혼합 모드 시연: 실제 시각·PR 없는 연결


def test_link_without_prs_marks_nothing(service) -> None:
    result = run(
        service.link_incident(
            context_id="ctx_inc_empty",
            requester="orchestrator",
            incident_context_id="ctx_inc_empty",
            pr_numbers=[],
            follow_up_title=None,
            idempotency_key="ctx_inc_empty:link_incident:none",
        )
    )
    assert result.output["linked_paras"] == []
    replies = [a.text for a in acts(service, "ctx_inc_empty") if a.to == "orchestrator"]
    assert replies and replies[-1] == "연결할 PR 없음 · 장애 표시 안 함"
    assert not any(p.incident == "ctx_inc_empty" for sp in service.state.specs for p in sp.paras)


def test_planning_clock_real_uses_wall_clock(monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from planning_agent.domain.clock import StepClock, default_clock_factory

    monkeypatch.delenv("PLANNING_CLOCK", raising=False)
    assert default_clock_factory() is StepClock
    monkeypatch.setenv("PLANNING_CLOCK", "real")
    clock = default_clock_factory()()
    assert abs(clock() - datetime.now(UTC)) < timedelta(seconds=5)
