"""독립 리뷰에서 나온 버그의 회귀 테스트: 키 재사용, 비활성 후임, 잠금, 트랜잭션, 교대, 충돌."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import Card, RequestMeta
from hr_agent.assignment import rules
from hr_agent.main import app
from hr_agent.planning_check.service import _decided_at
from hr_agent.results import make_evidence
from hr_agent.roster import store as roster
from hr_agent.roster.gaps import compute_gaps


def body(skill, inp, key="k", ctx="ctx_rev_0001", token="mock:orchestrator"):
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


def post(client, skill, inp, key="k", ctx="ctx_rev_0001", token="mock:orchestrator"):
    return client.post("/a2a", json=body(skill, inp, key, ctx, token)).json()


def request_change(client, kind, member, area, successor=None, key="a", ctx="ctx_rev_0001"):
    inp = {"change_kind": kind, "member_id": member, "area": area}
    if successor:
        inp["successor_id"] = successor
    return post(client, "request_assignment_change", inp, key, ctx)


def offboard(client, member, area, successor, ctx, key="SAME"):
    chg = request_change(client, "offboarding", member, area, successor, key, ctx)["output"][
        "change_id"
    ]
    post(client, "confirm_reassignment", {"change_id": chg, "successor_id": successor}, key, ctx)
    return chg


def approve_and_apply(svc, client, chg, ctx, key):
    held = post(client, "confirm_offboarding", {"change_id": chg}, key, ctx)
    svc.gate.decide_hold(held["output"]["hold_id"], approved=True)
    return post(client, "confirm_offboarding", {"change_id": chg}, key, ctx)


# 1) 같은 멱등성 키를 다른 대상에 쓰면 이미 받은 승인이 따라가면 안 된다 --------------------
def test_an_approval_does_not_carry_over_to_another_target_with_the_same_key(svc, client) -> None:
    a = offboard(client, "emp_01", "payment", "emp_02", "ctxA")
    b = offboard(client, "emp_03", "search", "emp_04", "ctxB")
    approve_and_apply(svc, client, a, "ctxA", "SAME")
    other = post(client, "confirm_offboarding", {"change_id": b}, "SAME", "ctxB")
    assert other["output"]["status"] == "held"  # 승인 없이 적용되지 않는다
    assert svc.store.one("SELECT active FROM members WHERE member_id = 'emp_03'")["active"] == 1


@pytest.mark.asyncio
async def test_gate_refuses_a_saved_key_reused_for_another_target(svc) -> None:
    meta = RequestMeta(
        context_id="ctx_g",
        token="t",
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        idempotency_key="K",
    )
    ev = make_evidence(svc.clock, "x")
    first = await svc.gate.execute("start_handover", {"target": "h1"}, meta, ev)
    again = await svc.gate.execute("start_handover", {"target": "h1"}, meta, ev)
    other = await svc.gate.execute("start_handover", {"target": "h2"}, meta, ev)
    assert first.status == again.status == "executed"
    assert other.status == "rejected"


# 2) 퇴직한 사람은 후임이 될 수 없다 -----------------------------------------------------
def test_an_offboarded_person_cannot_be_a_successor(svc, client) -> None:
    chg = offboard(client, "emp_01", "payment", "emp_02", "ctxA", "o")
    done = approve_and_apply(svc, client, chg, "ctxA", "o")
    assert done["output"]["status"] == "confirmed"
    res = request_change(client, "transfer", "emp_03", "search", "emp_01", "r", "ctxC")
    assert res["outcome"] == "UNKNOWN_MEMBER"
    start = post(
        client, "start_handover", {"owner_id": "emp_03", "successor_id": "emp_01"}, "s", "ctxD"
    )
    assert start["outcome"] == "UNKNOWN_MEMBER"


# 3) 퇴직 보류가 거절·만료되면 잠금과 상태가 정리된다 -----------------------------------
def test_a_rejected_offboarding_releases_its_locks(svc, client) -> None:
    chg = offboard(client, "emp_06", "support-desk", "emp_07", "ctxA", "o")
    held = post(client, "confirm_offboarding", {"change_id": chg}, "o", "ctxA")
    svc.gate.decide_hold(held["output"]["hold_id"], approved=False)
    res = post(client, "confirm_offboarding", {"change_id": chg}, "o", "ctxA")
    assert res["status"] == "completed" and res["output"]["status"] == "rejected"
    assert svc.store.one("SELECT status FROM assignment_changes")["status"] == "cancelled"
    assert svc.store.all("SELECT * FROM locks") == []
    nxt = request_change(client, "shift", "emp_07", "support-desk", key="n", ctx="ctxB")
    assert nxt["outcome"] == "requested"
    assert (
        post(client, "confirm_offboarding", {"change_id": chg}, "o2", "ctxA")["outcome"]
        == "CANCELLED"
    )


def test_an_expired_hold_cancels_the_offboarding_the_same_way(svc, client) -> None:
    chg = offboard(client, "emp_06", "support-desk", "emp_07", "ctxA", "o")
    post(client, "confirm_offboarding", {"change_id": chg}, "o", "ctxA")
    svc.clock.advance(minutes=11)  # 보류 10분 자동 거절
    res = post(client, "confirm_offboarding", {"change_id": chg}, "o", "ctxA")
    assert res["output"]["status"] == "rejected"
    assert svc.store.all("SELECT * FROM locks") == []


def test_stale_locks_expire(svc, client) -> None:
    request_change(client, "transfer", "emp_01", "payment", key="a", ctx="ctxA")
    assert (
        request_change(client, "shift", "emp_02", "payment", key="b", ctx="ctxB")["outcome"]
        == "LOCKED"
    )
    svc.clock.advance(minutes=svc.settings.lock_ttl_minutes + 1)
    assert (
        request_change(client, "shift", "emp_02", "payment", key="b", ctx="ctxB")["outcome"]
        == "requested"
    )


# 5) 적용 도중 오류가 나면 명부가 반쯤 바뀌지 않는다 --------------------------------------
def test_a_failure_while_applying_leaves_the_roster_untouched(
    svc, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    chg = request_change(client, "transfer", "emp_01", "payment")["output"]["change_id"]
    before = [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)]
    real = roster.set_active
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(roster, "set_active", flaky)
    crashing = TestClient(app, raise_server_exceptions=False)
    res = crashing.post(
        "/a2a",
        json=body("confirm_reassignment", {"change_id": chg, "successor_id": "emp_02"}, "r"),
    ).json()
    assert res["status"] == "failed"
    monkeypatch.setattr(roster, "set_active", real)
    assert [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)] == before
    assert svc.store.one("SELECT status FROM assignment_changes")["status"] == "requested"


# 6) 동행 요청(인수인계)이 실패하면 변경도 잠금도 남지 않는다 ----------------------------
def test_failed_companion_request_leaves_no_change_or_lock(svc, client, monkeypatch) -> None:
    from hr_agent.handover import service as handover
    from hr_agent.results import refused

    async def broken(_svc, request):
        return refused(request, "UNKNOWN_MEMBER", "실패")

    monkeypatch.setattr(handover, "start_handover", broken)
    res = request_change(client, "transfer", "emp_01", "payment")
    assert res["outcome"] == "SUBREQUEST_FAILED"
    assert svc.store.all("SELECT * FROM assignment_changes") == []
    assert svc.store.all("SELECT * FROM locks") == []


# 7) 퇴직·이동한 사람의 교대는 공백 계산과 타이머에 세지 않는다 --------------------------
def test_transfer_moves_future_shifts_so_no_gap_appears(svc, client) -> None:
    chg = request_change(client, "transfer", "emp_01", "payment")["output"]["change_id"]
    post(client, "confirm_reassignment", {"change_id": chg, "successor_id": "emp_02"}, "r")
    assert "payment" not in compute_gaps(svc.store, svc.clock()).gap_areas
    assert {s["member_id"] for s in roster.shifts(svc.store, "payment")} == {"emp_02"}


def test_shifts_of_people_no_longer_in_the_area_are_ignored(svc) -> None:
    roster.set_active(svc.store, "catalog", "owner", "emp_05", False)
    assert roster.shifts(svc.store, "catalog") == []


# 8) 사람이 보류를 거절해도 '항목 누락'으로 보고하지 않는다 -------------------------------
def test_a_human_rejection_is_not_reported_as_missing_items(svc, client) -> None:
    start = post(client, "start_handover", {"owner_id": "emp_01"}, "s")
    hid = start["output"]["handover_id"]
    held = post(client, "confirm_handover", {"handover_id": hid}, "c")
    assert held["status"] == "input-required" and held["reason_code"] == "human_input"
    svc.gate.decide_hold(held["output"]["hold_id"], approved=False)
    res = post(client, "confirm_handover", {"handover_id": hid}, "c")
    assert res["output"]["status"] == "rejected" and res["output"]["missing_item_ids"] == []


def test_more_answers_open_a_fresh_confirmation_attempt(svc, client) -> None:
    start = post(client, "start_handover", {"owner_id": "emp_01"}, "s")
    hid = start["output"]["handover_id"]
    held = post(client, "confirm_handover", {"handover_id": hid}, "c")
    svc.gate.decide_hold(held["output"]["hold_id"], approved=False)
    for q in start["output"]["questions"]:
        post(
            client,
            "answer_handover_question",
            {"question_id": q["question_id"], "answer": "답"},
            "a" + q["gap_field"],
        )
    res = post(client, "confirm_handover", {"handover_id": hid}, "c")
    assert res["output"]["status"] == "confirmed"  # 같은 키여도 완성도가 달라져 새로 판정한다


# 9) 같은 사건에서 내용이 다른 요청은 조용히 중복 처리하지 않는다 --------------------------
def test_a_different_successor_in_the_same_context_is_a_conflict(svc, client) -> None:
    assert (
        request_change(client, "transfer", "emp_01", "payment", "emp_02")["outcome"] == "requested"
    )
    again = request_change(client, "transfer", "emp_01", "payment", "emp_10")
    assert again["outcome"] == "CONFLICT"


def test_a_different_answer_to_an_answered_question_is_a_conflict(svc, client) -> None:
    start = post(client, "start_handover", {"owner_id": "emp_01"}, "s")
    q = start["output"]["questions"][0]["question_id"]
    first = post(client, "answer_handover_question", {"question_id": q, "answer": "첫 답"}, "a1")
    same = post(client, "answer_handover_question", {"question_id": q, "answer": "첫 답"}, "a2")
    diff = post(client, "answer_handover_question", {"question_id": q, "answer": "다른 답"}, "a3")
    assert (first["outcome"], same["outcome"], diff["outcome"]) == (
        "answered",
        "duplicate",
        "CONFLICT",
    )


# 10) 경과 일수는 UTC 날짜로 센다 --------------------------------------------------------
def test_days_since_rejected_uses_utc_dates() -> None:
    kst = datetime(2026, 10, 22, 8, 0, tzinfo=timezone(timedelta(hours=9)))
    card = Card(
        card_id="card_x",
        kind="decision",
        title="t",
        body="b",
        context_id="ctx_x_0001",
        author="a",
        confidence="high",
        created_at=kst,
        extra={"decided_at": kst.isoformat()},
    )
    assert _decided_at(card).date().isoformat() == "2026-10-21"  # UTC로는 전날 23시


# 요청 본문의 author가 아니라 토큰의 요청자가 카드 작성자다 -----------------------------
def test_card_author_is_the_verified_requester(svc, client) -> None:
    rec = {
        "context_id": "ctx_au_0001",
        "kind": "spec_confirmed",
        "summary": "확정",
        "decision_reason": "이유",
        "alternatives_considered": ["대안"],
        "source_refs": ["pr:1", "area:payment"],
        "author": "finance",
    }
    res = post(client, "record_case", rec, "r", "ctx_au_0001", "mock:planning")
    row = svc.store.one(
        "SELECT data FROM mock_cards WHERE card_id = ?", (res["output"]["card_id"],)
    )
    assert '"author": "planning"' in row["data"] or '"author":"planning"' in row["data"]


def test_rules_still_block_a_plan_that_leaves_a_gap(svc) -> None:
    ops = rules.plan_ops("transfer", "emp_05", "catalog", "emp_05", [("owner", "emp_05")])
    assert rules.simulate(svc.store, ops)[0].gap_areas == ["catalog"]
