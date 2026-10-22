import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ax_agent_template import config
from ax_agent_template import execute as execute_module
from ax_agent_template.execute import SAFETY_TIMEOUT_SECONDS, execute
from ax_contracts.schemas import Evidence, ExecuteDecision, RequestMeta


def _meta() -> RequestMeta:
    return RequestMeta(
        context_id="ctx_req_test",
        token="ignored",
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        idempotency_key="ctx_req_test:issue_coupon:cust_1",
    )


@pytest.fixture
def safety_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AX_SAFETY_URL", "http://safety.test")
    config.set_agent_name("marketing")


def _use_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    original = execute_module._send_to_safety

    async def send(request):
        async with httpx.AsyncClient(transport=httpx.MockTransport(wrapped)) as client:
            return await original(request, http_client=client)

    monkeypatch.setattr(execute_module, "_send_to_safety", send)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grade", "status"), [("A", "executed"), ("C", "held"), ("B", "rejected")]
)
async def test_execute_returns_safety_decision(safety_url, monkeypatch, grade, status) -> None:
    decision = ExecuteDecision(execution_id="exe_1", grade=grade, status=status)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": decision.model_dump(mode="json"), "meta": {}})

    _use_transport(monkeypatch, handler)
    result = await execute("issue_coupon", {"count": 1}, _meta(), Evidence())
    assert (result.grade, result.status) == (grade, status)


@pytest.mark.asyncio
async def test_execute_sends_bearer_token_and_idempotency_key(safety_url, monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"execution_id": "exe_1", "grade": "A", "status": "executed"}
        return httpx.Response(200, json={"data": body, "meta": {}})

    seen = _use_transport(monkeypatch, handler)
    await execute("issue_coupon", {"count": 1}, _meta(), Evidence())
    request = seen[0]
    assert str(request.url) == "http://safety.test/api/safety/executions"
    assert request.headers["authorization"] == "Bearer mock:marketing"
    body = json.loads(request.content)
    assert body["idempotency_key"] == "ctx_req_test:issue_coupon:cust_1"
    assert body["requester"] == "marketing"


@pytest.mark.asyncio
async def test_execute_rejects_on_http_and_transport_errors(safety_url, monkeypatch) -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("느림")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 안 됨")

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"grade": "Z"}})

    for handler in (unauthorized, timeout, refused, garbage):
        monkeypatch.undo()
        monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
        monkeypatch.setenv("AX_SAFETY_URL", "http://safety.test")
        config.set_agent_name("marketing")
        _use_transport(monkeypatch, handler)
        result = await execute("issue_coupon", {}, _meta(), Evidence())
        assert result.status == "rejected"


def test_safety_timeout_exceeds_verify_timeout() -> None:
    assert SAFETY_TIMEOUT_SECONDS > 30
