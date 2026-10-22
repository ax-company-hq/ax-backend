import pytest

from ax_agent_template.llm import LLMClient, MockLLMClient
from data_agent.explain import Explanation, _Summary, explain

IMPACT = {"failed_order_count": 12, "affected_customer_count": 9, "damage_amount": 1234000}


def _mock(text: str) -> MockLLMClient:
    return MockLLMClient(responses={_Summary: _Summary(summary=text)})


class _Boom(LLMClient):
    async def structured(self, prompt, schema, untrusted=None):  # type: ignore[no-untyped-def]
        raise TimeoutError("llm timeout")


@pytest.mark.asyncio
async def test_correct_numbers_kept() -> None:
    text = "실패 주문 12건, 영향 고객 9명이 확인되었습니다."
    assert await explain("impact", IMPACT, _mock(text)) == Explanation(text, False)


@pytest.mark.asyncio
async def test_wrong_number_falls_back() -> None:
    result = await explain("impact", IMPACT, _mock("실패 주문 13건입니다."))
    assert result.fallback is True
    assert "실패 주문 12건" in result.summary
    assert "1,234,000원" in result.summary


@pytest.mark.asyncio
async def test_llm_exception_falls_back() -> None:
    result = await explain("impact", IMPACT, _Boom("data"))
    assert result.fallback is True
    assert "영향 고객 9명" in result.summary


@pytest.mark.asyncio
async def test_unregistered_response_falls_back() -> None:
    result = await explain("impact", IMPACT, MockLLMClient())
    assert result.fallback is True


@pytest.mark.asyncio
async def test_comma_amount_accepted() -> None:
    text = "피해 금액은 1,234,000원입니다."
    assert await explain("impact", IMPACT, _mock(text)) == Explanation(text, False)


@pytest.mark.asyncio
async def test_decimal_and_anomaly_template() -> None:
    facts = {"metric": "결제 실패율", "baseline": 0.5, "observed": 2.5, "ratio": 5.0}
    ok = await explain("anomaly", facts, _mock("기준선 0.5 대비 2.5로 5.0배입니다."))
    assert ok.fallback is False
    bad = await explain("anomaly", facts, _mock("기준선 0.5 대비 3.5입니다."))
    assert bad.fallback is True
    assert bad.summary == "결제 실패율 지표가 기준선 0.5 대비 2.5로 5.0배입니다."


@pytest.mark.asyncio
async def test_unknown_kind_lists_key_values() -> None:
    result = await explain("other", {"a": 1, "b": "x"}, _Boom("data"))
    assert result == Explanation("other: a=1, b=x", True)


@pytest.mark.asyncio
async def test_missing_keys_do_not_raise() -> None:
    result = await explain("impact", {}, _Boom("data"))
    assert result.fallback is True
    assert "피해 금액 -원" in result.summary


@pytest.mark.asyncio
async def test_prompt_forbids_changing_numbers() -> None:
    mock = _mock("실패 주문 12건입니다.")
    await explain("impact", IMPACT, mock)
    assert "숫자를 바꾸거나" in mock.calls[0]
    assert "단정하지" in mock.calls[0]
