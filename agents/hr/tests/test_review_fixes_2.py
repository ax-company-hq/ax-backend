"""2차 독립 리뷰 회귀 테스트: 판정 실패 구분, 후임 일치, 다시 열기, 만료, 중복, memory 응답."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import ExecuteDecision
from hr_agent.clients.errors import UpstreamError
from hr_agent.clients.real import HttpMemoryClient
from hr_agent.main import app
from hr_agent.roster import store as roster


def body(skill, inp, key="k", ctx="ctx_r2_0001", token="mock:orchestrator"):
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


def post(client, skill, inp, key="k", ctx="ctx_r2_0001"):
    return client.post("/a2a", json=body(skill, inp, key, ctx)).json()


def request_change(client, kind, member, area, successor=None, key="a", ctx="ctx_r2_0001"):
    inp = {"change_kind": kind, "member_id": member, "area": area}
    if successor:
        inp["successor_id"] = successor
    return post(client, "request_assignment_change", inp, key, ctx)


def offboard(client, member, area, successor, ctx, key="o"):
    chg = request_change(client, "offboarding", member, area, successor, key, ctx)["output"][
        "change_id"
    ]
    post(client, "confirm_reassignment", {"change_id": chg, "successor_id": successor}, key, ctx)
    return chg


def gate_unavailable(monkeypatch, svc) -> None:
    """템플릿 execute()가 safety에 닿지 못했을 때 돌려주는 거부(사유 없음)를 흉내 낸다."""

    async def down(*_a, **_k):
        return ExecuteDecision(execution_id="exe_unavailable", grade="C", status="rejected")

    monkeypatch.setattr(svc.gate, "execute", down)


# 1) 안전 레이어에 닿지 못한 것을 사람의 거절로 처리하지 않는다 ---------------------------
def test_unreachable_safety_does_not_cancel_an_offboarding(svc, client, monkeypatch) -> None:
    chg = offboard(client, "emp_06", "support-desk", "emp_07", "ctxA")
    locks = svc.store.all("SELECT key FROM locks")
    gate_unavailable(monkeypatch, svc)
    res = post(client, "confirm_offboarding", {"change_id": chg}, "o", "ctxA")
    assert (res["status"], res["outcome"]) == ("failed", "GATE_UNAVAILABLE")
    assert svc.store.one("SELECT status FROM assignment_changes")["status"] == "confirmed"
    assert svc.store.all("SELECT key FROM locks") == locks


def test_unreachable_safety_keeps_a_held_handover_held(svc, client, monkeypatch) -> None:
    start = post(client, "start_handover", {"owner_id": "emp_01"}, "s")
    hid = start["output"]["handover_id"]
    post(client, "confirm_handover", {"handover_id": hid}, "c")  # 보류
    gate_unavailable(monkeypatch, svc)
    res = post(client, "confirm_handover", {"handover_id": hid}, "c2")
    assert res["outcome"] == "GATE_UNAVAILABLE"
    assert svc.store.one("SELECT status FROM handovers")["status"] == "held"


# 2) 확정은 요청 때 정한 후임만 받는다 ------------------------------------------------------
def test_confirm_must_use_the_successor_chosen_at_request_time(svc, client) -> None:
    chg = request_change(client, "transfer", "emp_03", "search", "emp_04")["output"]["change_id"]
    before = [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)]
    res = post(client, "confirm_reassignment", {"change_id": chg, "successor_id": "emp_10"}, "r")
    assert res["outcome"] == "CONFLICT"
    assert [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)] == before
    ok = post(client, "confirm_reassignment", {"change_id": chg, "successor_id": "emp_04"}, "r2")
    assert ok["outcome"] == "confirmed"
    again = post(client, "confirm_reassignment", {"change_id": chg, "successor_id": "emp_04"}, "r3")
    assert again["outcome"] == "duplicate"


# 3) 다시 열기도 관문을 거치고 후임을 검증한다 ----------------------------------------------
def test_reopening_goes_through_the_gate_and_checks_the_successor(svc, client) -> None:
    chg = offboard(client, "emp_06", "support-desk", "emp_07", "ctxA")
    held = post(client, "confirm_offboarding", {"change_id": chg}, "o", "ctxA")
    svc.gate.decide_hold(held["output"]["hold_id"], approved=False)
    post(client, "confirm_offboarding", {"change_id": chg}, "o", "ctxA")  # 취소
    bad = request_change(client, "offboarding", "emp_06", "support-desk", "emp_99", "o", "ctxA")
    assert bad["outcome"] == "UNKNOWN_MEMBER"
    calls = len(svc.gate.calls)
    ok = request_change(client, "offboarding", "emp_06", "support-desk", "emp_07", "o", "ctxA")
    assert ok["outcome"] == "reopened" and len(svc.gate.calls) == calls + 1


# 4) 방치된 변경은 만료로 닫히고, 빈 계획은 확정되지 않는다 ---------------------------------
def test_an_expired_change_cannot_be_confirmed_and_does_not_collide(svc, client) -> None:
    first = request_change(client, "transfer", "emp_03", "search", key="a", ctx="ctxA")
    svc.clock.advance(minutes=svc.settings.lock_ttl_minutes + 1)
    second = request_change(client, "offboarding", "emp_03", "search", key="b", ctx="ctxB")
    assert second["outcome"] == "requested"
    first_id = first["output"]["change_id"]
    late = post(
        client,
        "confirm_reassignment",
        {"change_id": first_id, "successor_id": "emp_04"},
        "r",
        "ctxA",
    )
    assert late["outcome"] == "BAD_STATE"
    row = svc.store.one("SELECT status FROM assignment_changes WHERE change_id = ?", (first_id,))
    assert row["status"] == "expired"


def test_a_plan_that_applies_nothing_is_refused(svc, client) -> None:
    chg = request_change(client, "transfer", "emp_03", "search", "emp_04")["output"]["change_id"]
    roster.set_active(svc.store, "search", "owner", "emp_03", False)  # 그 사이 영역에서 빠졌다
    res = post(client, "confirm_reassignment", {"change_id": chg, "successor_id": "emp_04"}, "r")
    assert res["outcome"] == "NOTHING_TO_APPLY"


# 5) 이미 적용된 요청을 다시 보내면 거절이 아니라 중복이다 -----------------------------------
def test_repeating_an_applied_request_is_a_duplicate_not_a_refusal(svc, client) -> None:
    chg = request_change(client, "transfer", "emp_01", "payment", "emp_02")["output"]["change_id"]
    post(client, "confirm_reassignment", {"change_id": chg, "successor_id": "emp_02"}, "r")
    again = request_change(client, "transfer", "emp_01", "payment", "emp_02")  # 이미 영역에 없다
    assert again["outcome"] == "duplicate" and again["output"]["change_id"] == chg
    join = request_change(client, "join", "emp_10", "inventory", key="j", ctx="ctxJ")
    post(
        client,
        "confirm_reassignment",
        {"change_id": join["output"]["change_id"], "successor_id": "emp_10"},
        "jr",
        "ctxJ",
    )
    assert request_change(client, "join", "emp_10", "inventory", key="j", ctx="ctxJ")[
        "outcome"
    ] == ("duplicate")


# 8) 저장소·재질문 ----------------------------------------------------------------------
def test_store_update_returns_the_number_of_changed_rows(svc) -> None:
    assert svc.store.execute("UPDATE members SET active = 1 WHERE member_id = 'nobody'") == 0
    assert svc.store.execute("UPDATE members SET active = 1 WHERE member_id = 'emp_01'") == 1


def test_a_confirmed_handover_is_not_asked_again(svc, client) -> None:
    start = post(client, "start_handover", {"owner_id": "emp_01"}, "s")
    hid = start["output"]["handover_id"]
    for q in start["output"]["questions"]:
        post(
            client,
            "answer_handover_question",
            {"question_id": q["question_id"], "answer": "답"},
            "a" + q["gap_field"],
        )
    post(client, "confirm_handover", {"handover_id": hid}, "c")
    svc.store.execute("UPDATE handover_questions SET status = 'open' WHERE handover_id = ?", (hid,))
    svc.clock.advance(minutes=svc.settings.handover_question_ttl_minutes + 1)
    again = post(client, "start_handover", {"owner_id": "emp_01"}, "s")
    assert all("다시 묻습니다" not in q["question"] for q in again["output"].get("questions", []))


# 7) memory의 404·봉투 누락은 성공이 아니다 ---------------------------------------------------
@pytest.mark.asyncio
async def test_memory_404_and_missing_envelopes_are_not_treated_as_success() -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(404)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"error": "boom"})  # data 없음
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(odd))
    memory = HttpMemoryClient("http://memory.test", http_client=client)
    with pytest.raises(UpstreamError):
        await memory.set_status("card_a", "superseded", "card_b")
    with pytest.raises(UpstreamError):
        await memory.search_cards("x")
    with pytest.raises(UpstreamError):
        await memory.list_cards()
    assert await memory.get_card("card_zzz") is None  # 없는 카드를 조회할 때만 None이다
