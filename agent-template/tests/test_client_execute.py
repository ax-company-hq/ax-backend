from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from ax_agent_template import config
from ax_agent_template import execute as execute_module
from ax_agent_template.client import call_agent
from ax_agent_template.execute import execute
from ax_contracts.schemas import AgentRequest, Evidence, RequestMeta


def _request(deadline_in: float = 60.0, path: list[str] | None = None) -> AgentRequest:
    return AgentRequest(
        meta=RequestMeta(
            context_id="ctx_req_test",
            token="ignored",
            path=path or [],
            deadline=datetime.now(UTC) + timedelta(seconds=deadline_in),
            idempotency_key="ctx_req_test:echo:1",
        ),
        skill="echo",
        input={"n": 1},
    )


def _asgi(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://echo")


@pytest.mark.asyncio
async def test_call_agent_appends_own_name_to_path(app: FastAPI) -> None:
    config.set_agent_name(
        "orchestrator"
    )  # app을 만들면 이름이 echo_agent로 바뀌므로 호출자로 되돌린다
    async with _asgi(app) as http_client:
        result = await call_agent("http://echo", _request(), http_client=http_client)
    assert result.status == "completed"
    assert result.output["path"] == ["orchestrator"]
    assert result.meta.requester == "orchestrator"  # 호출자 토큰에서 꺼낸 값


@pytest.mark.asyncio
async def test_call_agent_expired_deadline_does_not_call(app: FastAPI) -> None:
    config.set_agent_name("orchestrator")

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("마감이 지났으면 호출하지 않는다")

    transport = httpx.MockTransport(fail)
    async with httpx.AsyncClient(transport=transport) as http_client:
        result = await call_agent("http://echo", _request(deadline_in=-1), http_client=http_client)
    assert (result.status, result.outcome) == ("canceled", "TIMEOUT")


@pytest.mark.asyncio
async def test_call_agent_maps_transport_errors(app: FastAPI) -> None:
    config.set_agent_name("orchestrator")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("느림")

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 안 됨")

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    cases = [
        (timeout, "canceled", "TIMEOUT"),
        (refused, "failed", "unreachable"),
        (server_error, "failed", "http_500"),
    ]
    for handler, status, outcome in cases:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            result = await call_agent("http://echo", _request(), http_client=http_client)
        assert (result.status, result.outcome) == (status, outcome)


@pytest.mark.asyncio
async def test_call_agent_to_itself_is_a_loop(app: FastAPI) -> None:
    # 호출자 이름 == 받는 쪽 이름이면 path에 받는 쪽 이름이 들어 있어 루프로 거부된다
    async with _asgi(app) as http_client:
        result = await call_agent("http://echo", _request(), http_client=http_client)
    assert (result.status, result.outcome) == ("rejected", "LOOP_DETECTED")


def test_agent_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.agent_url("planning") == "http://planning:8101"
    assert config.agent_url("shop-api") == "http://shop-api:8010"
    assert config.agent_url("http://localhost:8106/") == "http://localhost:8106"
    monkeypatch.setenv("AX_HR_URL", "http://localhost:9000/")
    assert config.agent_url("hr") == "http://localhost:9000"
    with pytest.raises(KeyError):
        config.agent_url("unknown")


@pytest.mark.asyncio
async def test_execute_in_mock_mode_is_a_grade_a_execution() -> None:
    config.set_agent_name("marketing")
    context = _request().meta
    decision = await execute("issue_coupon", {"count": 1}, context, Evidence())
    assert (decision.grade, decision.status) == ("A", "executed")


@pytest.mark.asyncio
async def test_execute_is_fail_closed_when_safety_is_not_reachable(real_mode) -> None:
    config.set_agent_name("marketing")
    decision = await execute("issue_coupon", {}, _request().meta, Evidence())
    assert decision.status == "rejected"


@pytest.mark.asyncio
async def test_execute_requires_idempotency_key() -> None:
    config.set_agent_name("marketing")
    meta = _request().meta.model_copy(update={"idempotency_key": None})
    with pytest.raises(ValueError, match="idempotency_key"):
        await execute("issue_coupon", {}, meta, Evidence())


@pytest.mark.asyncio
async def test_execute_sends_own_name_and_ignores_claimed_grade(monkeypatch) -> None:
    sent = []

    async def fake_send(request):
        sent.append(request)
        return await original(request)

    original = execute_module._send_to_safety
    monkeypatch.setattr(execute_module, "_send_to_safety", fake_send)
    config.set_agent_name("marketing")
    await execute("issue_coupon", {"count": 2}, _request().meta, Evidence())
    assert sent[0].requester == "marketing"
    assert sent[0].claimed_grade is None
    assert sent[0].idempotency_key == "ctx_req_test:echo:1"
