"""계산된 숫자(facts)에 대한 한두 문장 설명 생성.

- 숫자는 코드가 계산해서 넘기고, LLM은 문장만 쓴다.
- LLM 문장 속 숫자가 facts에 없으면 버리고 고정 템플릿으로 대체한다(fallback=True).
- LLM 예외(미설정·타임아웃 등)와 스키마 검증 실패도 템플릿으로 대체한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

from ax_agent_template.llm import LLMClient

# 1,234 / 1234 / 0.5 / 12.5 형태. 퍼센트 기호는 숫자 뒤에 붙으므로 숫자만 뽑으면 된다.
_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")

_PROMPT = (
    "아래 kind의 분석 결과를 한국어 한두 문장으로 설명해라.\n"
    "규칙: facts에 있는 숫자만 쓰고 숫자를 바꾸거나 새로 만들지 말 것. "
    "원인을 단정하지 말 것. 판단·추측·권고는 쓰지 말 것.\n"
    "kind: {kind}\nfacts: {facts}"
)


class _Summary(BaseModel):
    summary: str


@dataclass(frozen=True)
class Explanation:
    summary: str
    fallback: bool


def _extract_numbers(text: str) -> set[float]:
    """문장에서 숫자(쉼표·소수 포함)를 모두 뽑는다."""
    return {float(m.replace(",", "")) for m in _NUMBER_RE.findall(text)}


def _fact_numbers(facts: dict[str, int | float | str]) -> set[float]:
    """facts에서 허용 숫자 집합을 만든다. 문자열 값 안의 숫자도 포함한다."""
    allowed: set[float] = set()
    for value in facts.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            allowed.add(float(value))
        else:
            allowed |= _extract_numbers(str(value))
    return allowed


def _numbers_valid(summary: str, facts: dict[str, int | float | str]) -> bool:
    allowed = _fact_numbers(facts)
    return all(any(abs(n - a) < 1e-9 for a in allowed) for n in _extract_numbers(summary))


def _fmt(value: object, *, comma: bool = False) -> str:
    """값을 문자열로. 키가 없으면 '-'."""
    if value is None:
        return "-"
    if comma and isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def _template(kind: str, facts: dict[str, int | float | str]) -> str:
    if kind == "impact":
        return (
            f"장애 구간 실패 주문 {_fmt(facts.get('failed_order_count'))}건, "
            f"영향 고객 {_fmt(facts.get('affected_customer_count'))}명, "
            f"피해 금액 {_fmt(facts.get('damage_amount'), comma=True)}원입니다."
        )
    if kind == "anomaly":
        return (
            f"{_fmt(facts.get('metric'))} 지표가 기준선 {_fmt(facts.get('baseline'))} 대비 "
            f"{_fmt(facts.get('observed'))}로 {_fmt(facts.get('ratio'))}배입니다."
        )
    pairs = ", ".join(f"{k}={v}" for k, v in facts.items())
    return f"{kind}: {pairs}" if pairs else f"{kind}: 표시할 값이 없습니다."


async def explain(kind: str, facts: dict[str, int | float | str], llm: LLMClient) -> Explanation:
    """facts를 설명하는 문장을 만든다. 검증에 실패하면 템플릿(fallback=True)을 쓴다."""
    try:
        result = await llm.structured(_PROMPT.format(kind=kind, facts=facts), _Summary)
        summary = result.summary.strip()
    except Exception:  # LLM 미설정·타임아웃·검증 실패 모두 템플릿으로 대체
        return Explanation(_template(kind, facts), True)
    if not summary or not _numbers_valid(summary, facts):
        return Explanation(_template(kind, facts), True)
    return Explanation(summary, False)
