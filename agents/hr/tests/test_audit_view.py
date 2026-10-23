"""감사 로그 조회: 사건별 시간순 보기."""

import pytest
from fastapi.testclient import TestClient

from hr_agent.demo.server import demo_app


@pytest.fixture
def client() -> TestClient:
    with TestClient(demo_app) as c:
        c.post("/api/reset")
        yield c


def _call(client, skill, inp, ctx, key):
    return client.post(
        "/api/call",
        json={
            "skill": skill,
            "requester": "orchestrator",
            "input": inp,
            "context_id": ctx,
            "idempotency_key": key,
        },
    ).json()


def test_timeline_lists_what_hr_did_in_order(client) -> None:
    started = _call(client, "start_handover", {"owner_id": "emp_01"}, "ctx_aud_1", "a1")
    q = started["result"]["output"]["questions"][0]["question_id"]
    _call(client, "answer_handover_question", {"question_id": q, "answer": "답"}, "ctx_aud_1", "a2")
    body = client.get("/api/audit/ctx_aud_1").json()
    kinds = [e["kind"] for e in body["timeline"]]
    assert kinds == ["start_handover", "answer_handover_question"]
    assert [e["seq"] for e in body["timeline"]] == [1, 2]
    assert body["timeline"][0]["payload"]["completeness"] == 40


def test_contexts_are_listed_with_counts(client) -> None:
    _call(client, "start_handover", {"owner_id": "emp_01"}, "ctx_aud_2", "b1")
    _call(client, "get_roster", {}, "ctx_aud_3", "b2")
    ctxs = {c["context_id"]: c["entries"] for c in client.get("/api/audit").json()["contexts"]}
    assert ctxs["ctx_aud_2"] == 1


def test_unknown_context_has_an_empty_timeline(client) -> None:
    assert client.get("/api/audit/ctx_nope").json()["timeline"] == []
