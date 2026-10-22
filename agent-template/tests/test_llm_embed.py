import math

import httpx
import pytest

from ax_agent_template.llm import (
    MOCK_EMBED_DIM,
    LLMClient,
    LLMNotConfiguredError,
    LLMSettings,
    MockLLMClient,
)


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_embed_model_prefers_department_value() -> None:
    env = {"HR_EMBED_MODEL": "dept-embed", "EMBED_MODEL": "common-embed"}
    assert LLMSettings.from_env("hr", env).embed_model == "dept-embed"
    assert LLMSettings.from_env("memory", env).embed_model == "common-embed"
    assert LLMSettings.from_env("memory", {"EMBED_MODEL": ""}).embed_model is None


@pytest.mark.asyncio
async def test_mock_embed_is_deterministic_fixed_dim_and_normalized() -> None:
    client = MockLLMClient()
    first = await client.embed(["환불 정책 안내", "배송 지연 문의"])
    second = await client.embed(["환불 정책 안내", "배송 지연 문의"])
    assert first == second
    assert all(len(v) == MOCK_EMBED_DIM == 64 for v in first)
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0) for v in first)


@pytest.mark.asyncio
async def test_mock_embed_similar_sentences_are_closer() -> None:
    client = MockLLMClient()
    base, similar, unrelated = await client.embed(
        ["환불 정책 변경 안내", "환불 정책 안내 문서", "쿠폰 발급 한도 초과 오류"]
    )
    assert _cosine(base, similar) > _cosine(base, unrelated)


@pytest.mark.asyncio
async def test_real_embed_needs_configuration() -> None:
    with pytest.raises(LLMNotConfiguredError):
        await LLMClient("memory", LLMSettings()).embed(["a"])
    with pytest.raises(LLMNotConfiguredError):
        await LLMClient("memory", LLMSettings(embed_model="m")).embed(["a"])


@pytest.mark.asyncio
async def test_real_embed_calls_openai_compatible_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    settings = LLMSettings(base_url="http://llm/v1/", api_key="k", embed_model="m")
    vectors = await LLMClient("memory", settings).embed(["a", "b"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["url"] == "http://llm/v1/embeddings"
    assert seen["auth"] == "Bearer k"
