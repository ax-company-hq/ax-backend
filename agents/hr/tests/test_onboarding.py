"""온보딩: 읽을 순서·확인 목록·진행률."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import StartOnboardingOutput
from hr_agent.main import app

CTX = "ctx_onb_0001"


def _body(skill: str, inp: dict, key: str, token: str = "mock:orchestrator", ctx: str = CTX):
    return {
        "meta": {
            "context_id": ctx,
            "token": token,
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": f"{ctx}:{key}",
        },
        "skill": skill,
        "input": inp,
    }


@pytest.fixture
def client(svc) -> TestClient:
    return TestClient(app)


def _decide(client, summary: str, n: int) -> None:
    rec = {
        "context_id": f"ctx_dec_{n:04d}",
        "kind": "spec_confirmed",
        "summary": summary,
        "decision_reason": "이유가 있다",
        "alternatives_considered": ["다른 안"],
        "source_refs": [f"pr:{n}", "area:payment"],
        "author": "planning",
    }
    res = client.post(
        "/a2a",
        json=_body("record_case", rec, f"rc{n}", token="mock:planning", ctx=rec["context_id"]),
    )
    assert res.json()["status"] == "completed"


def test_steps_list_decision_cards_then_checklist(svc, client) -> None:
    _decide(client, "결제 재시도 2회 확정", 1)
    _decide(client, "결제 타임아웃 5초 확정", 2)
    res = client.post(
        "/a2a",
        json=_body("start_onboarding", {"member_id": "emp_10", "area": "payment"}, "start"),
    ).json()
    assert res["status"] == "completed"
    out = StartOnboardingOutput.model_validate(res["output"])
    titles = [s.title for s in out.steps]
    assert titles[0].startswith("payment 영역")
    reads = [s for s in out.steps if s.card_id]
    assert len(reads) == 2 and all(s.title.startswith("결정 읽기") for s in reads)
    assert [s.order for s in out.steps] == list(range(1, len(out.steps) + 1))
    assert out.progress == 0
    assert set(res["evidence"]["ref_ids"]) >= {s.card_id for s in reads}


def test_no_cards_says_so_instead_of_inventing(svc, client) -> None:
    res = client.post(
        "/a2a", json=_body("start_onboarding", {"member_id": "emp_10", "area": "search"}, "s")
    ).json()
    out = StartOnboardingOutput.model_validate(res["output"])
    assert not any(s.card_id for s in out.steps)
    assert "읽을 결정 카드 없음" in res["evidence"]["data_summary"]


def test_confirmed_handover_marks_its_step_done(svc, client) -> None:
    start = client.post(
        "/a2a", json=_body("start_handover", {"owner_id": "emp_01", "successor_id": "emp_10"}, "h")
    ).json()
    hid = start["output"]["handover_id"]
    for q in start["output"]["questions"]:
        client.post(
            "/a2a",
            json=_body(
                "answer_handover_question",
                {"question_id": q["question_id"], "answer": "답"},
                f"a-{q['gap_field']}",
            ),
        )
    client.post("/a2a", json=_body("confirm_handover", {"handover_id": hid}, "c"))
    res = client.post(
        "/a2a",
        json=_body(
            "start_onboarding",
            {"member_id": "emp_10", "area": "payment", "handover_id": hid},
            "onb",
        ),
    ).json()
    out = StartOnboardingOutput.model_validate(res["output"])
    done = [s for s in out.steps if s.done]
    assert [s.title for s in done] == ["인수인계 내용을 확인한다"]
    assert out.progress == round(100 / len(out.steps))


def test_same_start_twice_keeps_one_plan(svc, client) -> None:
    body = _body("start_onboarding", {"member_id": "emp_10", "area": "payment"}, "same")
    a, b = client.post("/a2a", json=body).json(), client.post("/a2a", json=body).json()
    assert a["output"] == b["output"]
    assert len(svc.store.all("SELECT * FROM onboardings")) == 1


def test_invalid_inputs(svc, client) -> None:
    unknown_area = client.post(
        "/a2a", json=_body("start_onboarding", {"member_id": "emp_10", "area": "zzz"}, "a")
    ).json()
    assert unknown_area["outcome"] == "UNKNOWN_AREA"
    blank = client.post(
        "/a2a", json=_body("start_onboarding", {"member_id": " ", "area": "payment"}, "b")
    ).json()
    assert blank["outcome"] == "INVALID_INPUT"
    wrong = client.post(
        "/a2a",
        json=_body(
            "start_onboarding",
            {"member_id": "emp_10", "area": "payment", "handover_id": "hnd_nope"},
            "c",
        ),
    ).json()
    assert wrong["outcome"] == "UNKNOWN_HANDOVER"
