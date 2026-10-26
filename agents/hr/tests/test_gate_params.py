"""safety 규칙(#186)이 읽는 gate params: 완성도·미배정·중복 건수와 요약."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import StartHandoverOutput
from hr_agent.main import app


@pytest.fixture
def client(svc) -> TestClient:
    return TestClient(app)


def _body(skill: str, inp: dict, key: str, ctx: str) -> dict:
    return {
        "meta": {
            "context_id": ctx,
            "token": "mock:orchestrator",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": f"{ctx}:{key}",
        },
        "skill": skill,
        "input": inp,
    }


def _spy(svc) -> list[tuple[str, dict]]:
    seen: list[tuple[str, dict]] = []
    original = svc.gate.execute

    async def execute(action, params, *args, **kwargs):
        seen.append((action, dict(params)))
        return await original(action, params, *args, **kwargs)

    svc.gate.execute = execute
    return seen


def test_confirm_handover_sends_completeness_percent_and_short_summary(svc, client) -> None:
    seen = _spy(svc)
    ctx = "ctx_hnd_0100"
    start = client.post(
        "/a2a",
        json=_body("start_handover", {"owner_id": "emp_01", "successor_id": "emp_02"}, "s", ctx),
    ).json()
    out = StartHandoverOutput.model_validate(start["output"])
    for q in out.questions:
        answer = {"question_id": q.question_id, "answer": f"{q.gap_field} 내용"}
        client.post("/a2a", json=_body("answer_handover_question", answer, f"a-{q.gap_field}", ctx))
    client.post("/a2a", json=_body("confirm_handover", {"handover_id": out.handover_id}, "c", ctx))
    _, params = next((a, p) for a, p in seen if a.startswith("confirm_handover"))
    assert isinstance(params["completeness_percent"], int)
    assert 0 <= params["completeness_percent"] <= 100
    assert len(params["summary"]) <= 200
