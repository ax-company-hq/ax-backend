"""대시보드 인사 화면용 조회 skill과 사람 입력 skill(온보딩 단계 완료, 후보 카드 승인·거절)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import (
    DecideCandidateCardOutput,
    GetAuditTimelineOutput,
    ListCandidateCardsOutput,
    ListHandoversOutput,
    ListOpenQuestionsOutput,
)
from hr_agent.clients.memory import card_state
from hr_agent.main import app


def body(skill, inp, key="k", ctx="ctx_dv_0001", token="mock:dashboard-api"):
    return {
        "meta": {
            "context_id": ctx,
            "token": token,
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": inp,
    }


@pytest.fixture
def client(svc) -> TestClient:
    return TestClient(app)


def post(client, skill, inp, key="k", ctx="ctx_dv_0001", token="mock:dashboard-api"):
    return client.post("/a2a", json=body(skill, inp, key, ctx, token)).json()


def start_handover(client, owner="emp_01", successor="emp_02", ctx="ctx_hnd_0001"):
    return post(
        client,
        "start_handover",
        {"owner_id": owner, "successor_id": successor},
        "s",
        ctx,
        "mock:orchestrator",
    )


def candidate_card(client, summary="근거 없이 정한 결정", ctx="ctx_cand_0001", area="payment"):
    rec = {
        "context_id": ctx,
        "kind": "decision",
        "summary": summary,
        "area": area,
        "author": "planning",
    }
    res = post(client, "record_case", rec, "r", ctx, "mock:planning")
    assert res["output"]["outcome"] == "candidate", res
    return res["output"]["card_id"]


# ---- 인수인계·질문 -------------------------------------------------------------------------
def test_list_handovers_shows_status_completeness_items_and_open_questions(svc, client) -> None:
    start_handover(client)
    start_handover(client, owner="emp_03", successor="emp_04", ctx="ctx_hnd_0002")
    res = post(client, "list_handovers", {})
    out = ListHandoversOutput.model_validate(res["output"])
    assert res["status"] == "completed" and len(out.handovers) == 2
    first = next(h for h in out.handovers if h.owner_id == "emp_01")
    assert (first.status, first.completeness, first.area) == ("input_required", 40, "payment")
    assert {i.item_id for i in first.items} == {"pr_412", "tkt_201", "dep_102"}
    assert len(first.open_questions) == 3
    only = ListHandoversOutput.model_validate(
        post(client, "list_handovers", {"area": "search"})["output"]
    )
    assert [h.owner_id for h in only.handovers] == ["emp_03"]
    none = ListHandoversOutput.model_validate(
        post(client, "list_handovers", {"status": "confirmed"})["output"]
    )
    assert none.handovers == []


def test_open_questions_disappear_once_answered_or_confirmed(svc, client) -> None:
    started = start_handover(client)
    questions = started["output"]["questions"]
    listed = ListOpenQuestionsOutput.model_validate(
        post(client, "list_open_questions", {})["output"]
    )
    assert len(listed.questions) == 3
    post(
        client,
        "answer_handover_question",
        {"question_id": questions[0]["question_id"], "answer": "답"},
        "a1",
    )
    after = ListOpenQuestionsOutput.model_validate(
        post(client, "list_open_questions", {"assignee_id": "emp_01"})["output"]
    )
    assert len(after.questions) == 2
    other = ListOpenQuestionsOutput.model_validate(
        post(client, "list_open_questions", {"assignee_id": "emp_09"})["output"]
    )
    assert other.questions == []


def test_dashboard_can_answer_a_question_like_the_contract_says(svc, client) -> None:
    started = start_handover(client)
    q = started["output"]["questions"][0]["question_id"]
    res = post(client, "answer_handover_question", {"question_id": q, "answer": "답변"}, "a")
    assert res["status"] == "completed" and res["output"]["completeness"] == 60


# ---- 온보딩 --------------------------------------------------------------------------------


# ---- 변경 요청 -----------------------------------------------------------------------------


# ---- 후보 카드 -----------------------------------------------------------------------------
def test_low_confidence_cards_are_stored_with_the_candidate_flag(svc, client) -> None:
    card_id = candidate_card(client)
    row = svc.store.one("SELECT data FROM mock_cards WHERE card_id = ?", (card_id,))
    assert '"candidate": true' in row["data"] or '"candidate":true' in row["data"]
    assert '"status": "active"' in row["data"] or '"status":"active"' in row["data"]
    out = ListCandidateCardsOutput.model_validate(
        post(client, "list_candidate_cards", {})["output"]
    )
    assert [c.card_id for c in out.cards] == [card_id] and out.cards[0].candidate is True
    assert out.cards[0].extra["area"] == "payment"  # RecordCase.area 필드를 썼다
    none = ListCandidateCardsOutput.model_validate(
        post(client, "list_candidate_cards", {"area": "search"})["output"]
    )
    assert none.cards == []


def test_approving_a_candidate_makes_it_active_and_supersedes_the_older_decision(
    svc, client
) -> None:
    old = post(
        client,
        "record_case",
        {
            "context_id": "ctx_old_0001",
            "kind": "spec_confirmed",
            "summary": "결제 재시도를 최대 2회로 확정했다",
            "decision_reason": "중복 결제 위험",
            "alternatives_considered": ["없음"],
            "source_refs": ["pr:1", "area:payment"],
            "author": "planning",
        },
        "o",
        "ctx_old_0001",
        "mock:planning",
    )["output"]["card_id"]
    cand = candidate_card(client, summary="결제 재시도를 최대 3회로 확정했다", ctx="ctx_new_0001")
    res = post(client, "decide_candidate_card", {"card_id": cand, "decision": "approve"}, "d")
    out = DecideCandidateCardOutput.model_validate(res["output"])
    assert (out.status, res["outcome"]) == ("active", "approved")
    card = next(c for c in svc.store.all("SELECT data FROM mock_cards") if cand in c["data"])
    assert '"status": "active"' in card["data"]
    older = svc.store.one("SELECT data FROM mock_cards WHERE card_id = ?", (old,))
    assert '"status": "superseded"' in older["data"]
    assert "card.approve" in svc.audit.kinds("ctx_dv_0001")


def test_rejecting_a_candidate_retires_it(svc, client) -> None:
    cand = candidate_card(client)
    res = post(
        client,
        "decide_candidate_card",
        {"card_id": cand, "decision": "reject", "note": "근거 없음"},
        "d",
    )
    assert res["output"]["status"] == "retired" and res["outcome"] == "rejected"
    listed = ListCandidateCardsOutput.model_validate(
        post(client, "list_candidate_cards", {})["output"]
    )
    assert listed.cards == []


def test_candidate_decisions_are_idempotent_and_do_not_flip(svc, client) -> None:
    cand = candidate_card(client)
    first = post(client, "decide_candidate_card", {"card_id": cand, "decision": "approve"}, "d1")
    again = post(client, "decide_candidate_card", {"card_id": cand, "decision": "approve"}, "d2")
    flip = post(client, "decide_candidate_card", {"card_id": cand, "decision": "reject"}, "d3")
    assert (first["outcome"], again["outcome"], flip["outcome"]) == (
        "approved",
        "duplicate",
        "NOT_CANDIDATE",
    )
    assert (
        post(
            client, "decide_candidate_card", {"card_id": "card_nope", "decision": "approve"}, "d4"
        )["outcome"]
        == "UNKNOWN_CARD"
    )


@pytest.mark.asyncio
async def test_card_state_reads_both_new_and_old_candidate_forms(svc) -> None:
    from ax_contracts.schemas import Card

    now = svc.clock()
    base = {
        "card_id": "card_x",
        "kind": "decision",
        "title": "t",
        "body": "b",
        "context_id": "ctx_x_0001",
        "author": "a",
        "confidence": "low",
        "created_at": now,
    }
    assert card_state(Card(**base, candidate=True)) == "candidate"
    assert card_state(Card(**base, extra={"candidate": True})) == "candidate"  # 옛 방식
    assert card_state(Card(**base)) == "active"
    retired = Card(**base, candidate=True, status="retired")
    assert card_state(retired) == "retired"  # 폐기된 카드는 후보여도 폐기가 우선


# ---- 감사 -----------------------------------------------------------------------------------
def test_audit_timeline_lists_what_hr_did_for_a_context(svc, client) -> None:
    start_handover(client, ctx="ctx_aud_0001")
    out = GetAuditTimelineOutput.model_validate(
        post(client, "get_audit_timeline", {"context_id": "ctx_aud_0001"})["output"]
    )
    assert [e.kind for e in out.entries] == ["start_handover"] and out.entries[0].seq == 1
    assert out.entries[0].payload["completeness"] == 40
    empty = post(client, "get_audit_timeline", {"context_id": "ctx_none_0001"})
    assert empty["output"]["entries"] == []
    assert "확인할 수 없음" in empty["evidence"]["data_summary"]


# ---- 권한 ----------------------------------------------------------------------------------
