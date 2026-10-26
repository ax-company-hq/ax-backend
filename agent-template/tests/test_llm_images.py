"""LLMClient.structured 이미지 입력 — 요청 모양, 검증, 개인정보(바이트 비기록), 재요청, 목 일치."""

import base64
import json

import httpx
import pytest
from pydantic import BaseModel

from ax_agent_template.llm import (
    IMAGE_NOTICE,
    MAX_IMAGE_BYTES,
    MAX_IMAGES,
    ImageInput,
    LLMClient,
    LLMInputError,
    LLMResponseError,
    LLMSettings,
    MockLLMClient,
    build_messages,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"receipt-secret-card-1234-5678"


class Receipt(BaseModel):
    store: str
    total: int


SETTINGS = LLMSettings(model="gemini-test", base_url="https://llm.test/v1beta/openai", api_key="k")


def _client(handler, dept: str = "support") -> LLMClient:
    return LLMClient(dept, SETTINGS, transport=httpx.MockTransport(handler))


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_messages_without_images_are_plain_text_as_before() -> None:
    messages = build_messages("질문", {"고객 문의": "환불해 줘"})
    assert messages[0]["role"] == "user" and isinstance(messages[0]["content"], str)
    assert "<untrusted_data>" in messages[0]["content"]
    assert "image_url" not in json.dumps(messages)


def test_messages_with_images_use_base64_data_urls_and_the_untrusted_notice() -> None:
    messages = build_messages("영수증을 읽어", None, [ImageInput(PNG, "image/png")])
    parts = messages[0]["content"]
    assert parts[0]["type"] == "text" and IMAGE_NOTICE in parts[0]["text"]
    url = parts[1]["image_url"]["url"]
    assert url == "data:image/png;base64," + base64.b64encode(PNG).decode()


@pytest.mark.parametrize(
    "image, match",
    [
        (ImageInput(b"x", "application/pdf"), "지원하지 않는 이미지 형식"),
        (ImageInput(b"", "image/png"), "비어 있다"),
        (ImageInput(b"x" * (MAX_IMAGE_BYTES + 1), "image/png"), "너무 크다"),
    ],
)
def test_invalid_images_are_rejected_before_any_request(image: ImageInput, match: str) -> None:
    with pytest.raises(LLMInputError, match=match):
        build_messages("p", None, [image])


def test_too_many_images_are_rejected() -> None:
    images = [ImageInput(b"x", "image/png")] * (MAX_IMAGES + 1)
    with pytest.raises(LLMInputError, match="장까지"):
        build_messages("p", None, images)


def test_image_bytes_do_not_leak_into_repr_or_error_text() -> None:
    image = ImageInput(PNG, "image/png")
    assert b"secret".decode() not in repr(image)
    assert "secret" not in image.describe()
    with pytest.raises(LLMInputError) as caught:
        build_messages("p", None, [ImageInput(PNG, "image/gif")])
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_structured_sends_images_and_validates_the_answer() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers["authorization"],
                "body": json.loads(request.content),
            }
        )
        return _ok('{"store": "캣북스", "total": 12000}')

    out = await _client(handler).structured("읽어", Receipt, images=[ImageInput(PNG, "image/png")])
    assert out == Receipt(store="캣북스", total=12000)
    request = seen[0]
    assert request["url"] == "https://llm.test/v1beta/openai/chat/completions"
    assert request["auth"] == "Bearer k"
    body = request["body"]
    assert body["model"] == "gemini-test" and body["response_format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_text_only_call_still_works_without_images() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert isinstance(json.loads(request.content)["messages"][0]["content"], str)
        return _ok('```json\n{"store": "a", "total": 1}\n```')  # 코드 블록으로 감싼 JSON도 받는다

    assert (await _client(handler).structured("p", Receipt)).total == 1


@pytest.mark.asyncio
async def test_invalid_answer_is_requested_again_then_fails() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _ok('{"store": "a"}')  # total 없음

    with pytest.raises(LLMResponseError, match="2번 요청"):
        await _client(handler).structured("p", Receipt)
    assert calls["n"] == 2  # 기본 재요청 1번


@pytest.mark.asyncio
async def test_second_attempt_can_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_LLM_MAX_RETRIES", "2")
    answers = iter(["not json", '{"store": "a"}', '{"store": "a", "total": 5}'])

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(next(answers))

    assert (await _client(handler).structured("p", Receipt)).total == 5


@pytest.mark.asyncio
async def test_api_error_does_not_echo_the_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="echo receipt-secret-card-1234-5678")

    with pytest.raises(LLMResponseError) as caught:
        await _client(handler).structured("p", Receipt, images=[ImageInput(PNG, "image/png")])
    assert "400" in str(caught.value) and "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_invalid_image_is_rejected_without_calling_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("요청을 보내면 안 된다")

    with pytest.raises(LLMInputError):
        await _client(handler).structured("p", Receipt, images=[ImageInput(b"x", "image/gif")])


@pytest.mark.asyncio
async def test_mock_client_accepts_images_validates_and_records_no_bytes() -> None:
    mock = MockLLMClient(responses={Receipt: Receipt(store="s", total=1)})
    out = await mock.structured("읽어", Receipt, images=[ImageInput(PNG, "image/png")])
    assert out.total == 1
    recorded = mock.calls[0]
    assert "[image: image/png," in recorded and IMAGE_NOTICE in recorded
    assert "secret" not in recorded and base64.b64encode(PNG).decode() not in recorded
    assert len(mock.image_calls) == 1
    with pytest.raises(LLMInputError):
        await mock.structured("읽어", Receipt, images=[ImageInput(b"x", "image/gif")])


@pytest.mark.asyncio
async def test_mock_client_without_images_is_unchanged() -> None:
    mock = MockLLMClient(responses={Receipt: Receipt(store="s", total=1)})
    await mock.structured("p", Receipt)
    assert mock.image_calls == [] and "[image:" not in mock.calls[0]
