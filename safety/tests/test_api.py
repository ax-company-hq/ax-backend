"""HTTP API·인증·우회 방지 테스트 (단독 테스트 ⑦, SAF-07, COM-14)."""

from __future__ import annotations

from typing import Any

import pytest

from ax_agent_template import events
from ax_contracts.schemas import AgentResult, EventType, RequestMeta, TaskStatus
from ax_safety import verify
from ax_safety.gate import VerifyTask

MARKETING = {"Authorization": "Bearer mock:marketing"}
HUMAN = {"Authorization": "Bearer human:taeyoon"}


def _post(client: Any, body: dict, headers: dict | None = MARKETING) -> Any:
    return client.post("/api/safety/executions", json=body, headers=headers or {})


def _hold_something(client: Any, coupon: Any) -> str:
    body = coupon(20, 20_000, purpose_code="campaign", key="big").model_dump(mode="json")
    data = _post(client, body).json()["data"]
    assert data["status"] == "held"
    return data["review_id"]


def test_execute_over_http(client: Any, coupon: Any) -> None:
    response = _post(client, coupon().model_dump(mode="json"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["grade"], data["status"], data["computed_amount"]) == ("A", "executed", 20_000)
    listed = client.get("/api/safety/executions", params={"context_id": "ctx_inc_0001"}).json()
    assert [r["execution_id"] for r in listed["data"]] == [data["execution_id"]]


@pytest.mark.parametrize("headers", [None, {"Authorization": "Bearer "}, {"Authorization": "x"}])
def test_request_without_token_is_refused(client: Any, coupon: Any, headers: Any) -> None:
    """SAF-07: 토큰 없는 요청은 판정까지 가지 않는다."""
    response = client.post(
        "/api/safety/executions", json=coupon().model_dump(mode="json"), headers=headers or {}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert client.get("/api/safety/executions").json()["data"] == []
    steps = [e["step"] for e in client.get("/api/safety/audit-logs").json()["data"]]
    assert steps == ["auth_rejected"]  # 인증 거부도 감사 로그에 남는다 (SAF-06)


def test_requester_cannot_be_spoofed(client: Any, coupon: Any) -> None:
    """support 토큰으로 본문에 requester=marketing을 적어도 거부한다 (COM-14)."""
    response = _post(
        client, coupon().model_dump(mode="json"), {"Authorization": "Bearer mock:support"}
    )
    assert response.status_code == 403
    assert client.get("/api/safety/executions").json()["data"] == []


def test_non_mock_tokens_fail_closed(
    client: Any, coupon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK")
    response = _post(client, coupon().model_dump(mode="json"))
    assert response.status_code == 401


def test_malformed_request_is_400(client: Any) -> None:
    response = _post(client, {"action": "issue_coupon"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_agent_token_cannot_decide_reviews(client: Any, coupon: Any) -> None:
    """⑦ 에이전트 토큰으로 결정 API를 부르면 거부되고 보류 상태가 그대로다."""
    review_id = _hold_something(client, coupon)
    url = f"/api/safety/reviews/{review_id}/decision"
    for headers in (MARKETING, {"Authorization": "Bearer mock:finance"}, {}):
        response = client.post(url, json={"decision": "approved"}, headers=headers)
        assert response.status_code == 403
    review = client.get(f"/api/safety/reviews/{review_id}").json()["data"]
    assert review["status"] == "pending"


def test_human_decides_review(client: Any, coupon: Any) -> None:
    review_id = _hold_something(client, coupon)
    pending = client.get("/api/safety/reviews", params={"status": "pending"}).json()["data"]
    assert [r["review_id"] for r in pending] == [review_id]
    assert pending[0]["report"]["calculation"] == "20 × 20,000 = 400,000원"

    url = f"/api/safety/reviews/{review_id}/decision"
    response = client.post(url, json={"decision": "rejected", "note": "금액 과다"}, headers=HUMAN)
    assert response.status_code == 200
    assert (response.json()["data"]["status"], response.json()["data"]["decided_by"]) == (
        "rejected",
        "taeyoon",
    )
    again = client.post(url, json={"decision": "approved"}, headers=HUMAN)
    assert again.status_code == 409
    assert (
        client.post(
            "/api/safety/reviews/rev_x/decision", json={"decision": "approved"}, headers=HUMAN
        ).status_code
        == 404
    )


def test_configured_human_tokens(client: Any, coupon: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFETY_HUMAN_TOKENS", "byunghwan:s3cret")
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK")
    monkeypatch.setenv("SAFETY_MOCK", "1")  # 실행 요청은 목 토큰으로 만든다
    review_id = _hold_something(client, coupon)
    url = f"/api/safety/reviews/{review_id}/decision"
    monkeypatch.delenv("SAFETY_MOCK")
    assert client.post(url, json={"decision": "approved"}, headers=HUMAN).status_code == 403
    ok = client.post(url, json={"decision": "approved"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.json()["data"]["decided_by"] == "byunghwan"


def test_read_endpoints(client: Any) -> None:
    thresholds = client.get("/api/safety/thresholds").json()["data"]
    assert thresholds["version"] == 1
    registry = client.get("/api/safety/registry").json()["data"]
    assert {a["action"] for a in registry["actions"]} >= {"issue_coupon", "publish_notice"}
    assert client.get("/health").json() == {"data": {"status": "ok"}, "meta": {}}


@pytest.mark.asyncio
async def test_call_verifier_maps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Any] = []

    def _reply(status: TaskStatus, output: dict) -> Any:
        async def fake(target: str, req: Any) -> AgentResult:
            sent.append((target, req))
            meta = RequestMeta.model_validate(req.meta.model_dump())
            return AgentResult(meta=meta, status=status, output=output)

        return fake

    task = VerifyTask(
        verifier="finance",
        skill="verify_amount",
        context_id="ctx_inc_0001",
        tenant_id="shop-demo",
        execution_id="exe_1",
        timeout_seconds=30,
        input={"execution_id": "exe_1"},
    )
    cases = [
        (TaskStatus.COMPLETED, {"decision": "approved"}, "approved"),
        (TaskStatus.COMPLETED, {"approved": True}, "approved"),
        (TaskStatus.COMPLETED, {"decision": "rejected", "reason_code": "x"}, "rejected"),
        (TaskStatus.COMPLETED, {}, "rejected"),
        (TaskStatus.CANCELED, {}, "unavailable"),
        (TaskStatus.FAILED, {}, "unavailable"),
    ]
    for status, output, expected in cases:
        monkeypatch.setattr(verify, "call_agent", _reply(status, output))
        assert (await verify.call_verifier(task)).result == expected
    target, req = sent[0]
    assert (target, req.skill, req.meta.idempotency_key) == (
        "finance",
        "verify_amount",
        "verify:exe_1",
    )


@pytest.mark.asyncio
async def test_safety_events_go_to_the_bus(coupon: Any) -> None:
    await verify.publish_safety_event(
        "safety.action_held",
        coupon(),
        {
            "execution_id": "exe_1",
            "action": "issue_coupon",
            "requester": "marketing",
            "grade": "C",
            "computed_amount": 400000,
            "threshold_version": 1,
            "hold_reason": "over_threshold",
            "review_id": "rev_1",
        },
    )
    envelope = events.MOCK_PUBLISHED[-1]
    assert envelope.type == EventType.SAFETY_ACTION_HELD
    assert (envelope.source, envelope.context_id) == ("safety", "ctx_inc_0001")
    assert envelope.idempotency_key == "safety.action_held:exe_1"


def test_dashboard_origin_is_allowed(client: Any) -> None:
    """dashboard-web(3001)이 보류 큐를 읽고 결정을 보낼 수 있다. 다른 출처는 허용하지 않는다."""
    ok = client.options(
        "/api/safety/reviews/rev_1/decision",
        headers={
            "Origin": "http://localhost:3001",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3001"
    other = client.get("/api/safety/reviews", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


@pytest.mark.asyncio
async def test_safety_events_follow_contract_models(coupon: Any) -> None:
    """#91: 발행 data는 contracts 모델(SafetyActionHeldData 등) 형식이고, 틀리면 발행하지 않는다."""
    from pydantic import ValidationError

    from ax_contracts.schemas import SafetyActionHeldData

    data = {
        "execution_id": "exe_9",
        "action": "issue_coupon",
        "requester": "marketing",
        "grade": "C",
        "computed_amount": 400000,
        "threshold_version": 1,
        "hold_reason": "over_threshold",
        "review_id": "rev_9",
    }
    await verify.publish_safety_event("safety.action_held", coupon(), data)
    assert SafetyActionHeldData.model_validate(events.MOCK_PUBLISHED[-1].data).review_id == "rev_9"
    with pytest.raises(ValidationError):
        await verify.publish_safety_event("safety.action_held", coupon(), {"execution_id": "x"})
