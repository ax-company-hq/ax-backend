import json
import logging

import httpx
import pytest

from ax_agent_template.llm import LLM_CALL_KIND, LLMClient, LLMSettings, MockLLMClient


def _usage_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "ax.event"]
    return [e for e in events if e["kind"] == LLM_CALL_KIND]


@pytest.mark.asyncio
async def test_mock_embed_logs_usage_with_zero_tokens(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ax.event")
    await MockLLMClient(dept="memory").embed(["고객 원문"], context_id="ctx-1")
    [event] = _usage_events(caplog)
    assert event["context_id"] == "ctx-1"
    assert event["tenant_id"] == "shop-demo"
    assert event["payload"] == {
        "department": "memory",
        "model": "mock",
        "input_tokens": 0,
        "output_tokens": 0,
        "success": True,
    }
    assert "고객 원문" not in json.dumps(event, ensure_ascii=False)


@pytest.mark.asyncio
async def test_real_embed_logs_token_counts_per_context(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ax.event")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [1.0]}],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler))
    )
    client = LLMClient("memory", LLMSettings(base_url="http://llm/v1", embed_model="m"))
    await client.embed(["a"], context_id="ctx-2")
    await client.embed(["b"], context_id="ctx-2")
    events = _usage_events(caplog)
    assert len(events) == 2
    assert sum(e["payload"]["input_tokens"] for e in events) == 14  # type: ignore[index]
    assert {e["context_id"] for e in events} == {"ctx-2"}


@pytest.mark.asyncio
async def test_failed_real_embed_logs_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ax.event")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    client = LLMClient("memory", LLMSettings(base_url="http://llm/v1", embed_model="m"))
    with pytest.raises(httpx.HTTPStatusError):
        await client.embed(["a"], context_id="ctx-3")
    [event] = _usage_events(caplog)
    assert event["payload"]["success"] is False  # type: ignore[index]
