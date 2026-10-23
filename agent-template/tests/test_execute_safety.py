from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ax_agent_template import config
from ax_agent_template import execute as execute_module
from ax_agent_template.execute import SAFETY_TIMEOUT_SECONDS, execute
from ax_contracts.schemas import Evidence, ExecuteDecision, RequestMeta


@pytest.fixture
def safety_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = "http://safety.test"
    monkeypatch.setenv("AX_SAFETY_URL", url)
    config.set_agent_name("marketing")
    return url


def _meta() -> RequestMeta:
    return RequestMeta(
        context_id="ctx_safety",
        token="ignored",
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        idempotency_key="ctx_safety:issue_coupon:1",
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(execute_module.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grade", "status", "extra"),
    [
        ("A", "executed", {}),
        ("C", "held", {"hold_reason": "over_threshold", "review_id": "rev_1"}),
        ("B", "rejected", {}),
    ],
)
async def test_execute_returns_safety_decision(
    safety_url, monkeypatch, grade, status, extra
) -> None:
    decision = ExecuteDecision(execution_id="exe_1", grade=grade, status=status, **extra)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": decision.model_dump(mode="json"), "meta": {}})

    _patch_transport(monkeypatch, handler)
    result = await execute("issue_coupon", {"count": 1}, _meta(), Evidence())
    assert result == decision
    assert str(seen[0].url) == f"{safety_url}/api/safety/executions"


@pytest.mark.asyncio
async def test_execute_sends_bearer_token_and_idempotency_key(safety_url, monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = ExecuteDecision(execution_id="exe_1", grade="A", status="executed")
        return httpx.Response(200, json={"data": body.model_dump(mode="json"), "meta": {}})

    _patch_transport(monkeypatch, handler)
    await execute("issue_coupon", {}, _meta(), Evidence())
    request = seen[0]
    assert request.headers["authorization"] == "Bearer mock:marketing"
    import json

    body = json.loads(request.content)
    assert body["idempotency_key"] == "ctx_safety:issue_coupon:1"
    assert body["requester"] == "marketing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(401),
        httpx.Response(403),
        httpx.Response(400),
        httpx.ReadTimeout("느림"),
        httpx.ConnectError("연결 안 됨"),
    ],
)
async def test_execute_is_rejected_when_safety_fails(safety_url, monkeypatch, failure) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, Exception):
            raise failure
        return failure

    _patch_transport(monkeypatch, handler)
    result = await execute("issue_coupon", {}, _meta(), Evidence())
    assert result.status == "rejected"


def test_safety_timeout_is_longer_than_verify_timeout() -> None:
    assert SAFETY_TIMEOUT_SECONDS > 30
