"""카드 임베딩 인터페이스와 구현 (#47, MEM-01).

모델 이름과 엔드포인트는 `.env`로 정한다(`MEMORY_EMBED_MODEL` 등). 코드에 모델을 박지 않는다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol

from ax_agent_template.llm import LLMClient, MockLLMClient
from ax_contracts.schemas.record import Card


class Embedder(Protocol):
    """텍스트 목록을 벡터 목록으로 바꾼다. 입력 순서를 유지한다."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class LLMEmbedder:
    """`LLMClient.embed`에 위임하는 임베더."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._llm.embed(texts)


def get_embedder(env: Mapping[str, str] | None = None) -> Embedder:
    """`MEMORY_MOCK=1`이면 네트워크 없는 결정적 목 임베더, 아니면 실제 LLM 임베더를 돌려준다."""
    source = os.environ if env is None else env
    if source.get("MEMORY_MOCK") == "1":
        return LLMEmbedder(MockLLMClient())
    return LLMEmbedder(LLMClient("memory"))


def card_text(card: Card) -> str:
    """임베딩 대상 문장: 제목, 본문, 태그를 줄바꿈으로 잇는다."""
    parts = [card.title, card.body]
    if card.tags:
        parts.append(" ".join(card.tags))
    return "\n".join(parts)
