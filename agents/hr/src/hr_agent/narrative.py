"""LLM 문장 생성과 안전한 대체(fallback).

원칙: 숫자·사실은 코드가 정하고 LLM은 해석 문장만 쓴다.
- LLM이 없으면(목 모드) 고정 템플릿을 그대로 쓴다. 이때는 `fallback=False`다(LLM을 시도하지 않음).
- LLM을 불렀는데 실패하거나, 스키마가 안 맞거나, 문장 속 숫자가 계산값과 다르면
  문장을 버리고 고정 템플릿을 쓰고 `fallback=True`를 돌려준다.
- 외부 입력(`untrusted`)은 프롬프트의 데이터 블록에만 넣고 지시로 해석하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ax_agent_template.llm import LLMClient
from hr_agent.util import numbers_in


class NarrativeOut(BaseModel):
    """LLM이 돌려주는 문장 하나."""

    text: str = Field(min_length=1, max_length=600)


@dataclass(frozen=True)
class Sentence:
    text: str
    fallback: bool  # LLM을 시도했지만 템플릿으로 대체했으면 true


def _allowed_numbers(facts: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()
    for value in facts.values():
        allowed |= numbers_in(str(value))
    return allowed


class Narrator:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    async def sentence(
        self,
        purpose: str,
        facts: dict[str, Any],
        template: str,
        untrusted: dict[str, Any] | None = None,
    ) -> Sentence:
        """`template`은 사실 필드만으로 만든 고정 문장이다. LLM 문장은 이 사실과 어긋나면 버린다."""
        if self._llm is None:
            return Sentence(template, fallback=False)
        prompt = (
            f"다음 사실만 사용해서 {purpose}을(를) 한두 문장의 한국어로 쓴다. "
            f"사실에 없는 숫자·이름·원인은 쓰지 않는다.\n사실: {facts}"
        )
        try:
            out = await self._llm.structured(prompt, NarrativeOut, untrusted=untrusted)
        except Exception:  # noqa: BLE001 - 어떤 실패든 템플릿으로 대체한다
            return Sentence(template, fallback=True)
        if not numbers_in(out.text) <= _allowed_numbers(facts):
            return Sentence(template, fallback=True)  # 계산값에 없는 숫자가 들어 있다
        return Sentence(out.text.strip(), fallback=False)
