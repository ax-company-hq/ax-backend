"""2차 독립 리뷰 회귀 테스트: 판정 실패 구분, 후임 일치, 다시 열기, 만료, 중복, memory 응답."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import ExecuteDecision
from hr_agent.clients.errors import UpstreamError
from hr_agent.clients.real import HttpMemoryClient
from hr_agent.main import app


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


def gate_unavailable(monkeypatch, svc) -> None:
    """템플릿 execute()가 safety에 닿지 못했을 때 돌려주는 거부(사유 없음)를 흉내 낸다."""

    async def down(*_a, **_k):
        return ExecuteDecision(execution_id="exe_unavailable", grade="C", status="rejected")

    monkeypatch.setattr(svc.gate, "execute", down)


# 1) 안전 레이어에 닿지 못한 것을 사람의 거절로 처리하지 않는다 ---------------------------


def test_unreachable_safety_keeps_a_held_handover_held(svc, client, monkeypatch) -> None:
    start = post(client, "start_handover", {"owner_id": "emp_01"}, "s")
    hid = start["output"]["handover_id"]
    post(client, "confirm_handover", {"handover_id": hid}, "c")  # 보류
    gate_unavailable(monkeypatch, svc)
    res = post(client, "confirm_handover", {"handover_id": hid}, "c2")
    assert res["outcome"] == "GATE_UNAVAILABLE"
    assert svc.store.one("SELECT status FROM handovers")["status"] == "held"


# 2) 확정은 요청 때 정한 후임만 받는다 ------------------------------------------------------


# 3) 다시 열기도 관문을 거치고 후임을 검증한다 ----------------------------------------------


# 4) 방치된 변경은 만료로 닫히고, 빈 계획은 확정되지 않는다 ---------------------------------


# 5) 이미 적용된 요청을 다시 보내면 거절이 아니라 중복이다 -----------------------------------


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
