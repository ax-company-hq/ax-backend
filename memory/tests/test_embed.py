"""임베더 테스트. 네트워크 없이 돈다.

memory 패키지에는 async 테스트 플러그인이 없어 `asyncio.run`으로 돌린다.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime

from ax_agent_template.llm import MockLLMClient
from ax_contracts.schemas.record import Card
from ax_memory.embed import LLMEmbedder, card_text, get_embedder


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_같은_입력은_같은_벡터이고_차원이_일정하다() -> None:
    embedder = LLMEmbedder(MockLLMClient())
    first = asyncio.run(embedder.embed(["결제 API 장애", "쿠폰 정책 변경"]))
    second = asyncio.run(embedder.embed(["결제 API 장애", "쿠폰 정책 변경"]))
    assert first == second
    assert len({len(v) for v in first}) == 1
    assert math.isclose(math.sqrt(sum(x * x for x in first[0])), 1.0)


def test_비슷한_문장이_무관한_문장보다_가깝다() -> None:
    embedder = LLMEmbedder(MockLLMClient())
    base, similar, other = asyncio.run(
        embedder.embed(
            [
                "결제 API 장애로 주문 실패",
                "결제 API 장애 발생 주문 실패 증가",
                "신규 쿠폰 발급 이벤트 안내",
            ]
        )
    )
    assert _cos(base, similar) > _cos(base, other)


def test_mock_환경이면_네트워크_없는_임베더를_고른다() -> None:
    embedder = get_embedder({"MEMORY_MOCK": "1"})
    vectors = asyncio.run(embedder.embed(["테스트"]))
    assert len(vectors) == 1 and vectors[0]


def test_mock이_아니면_LLM_임베더를_고른다() -> None:
    embedder = get_embedder({})
    assert isinstance(embedder, LLMEmbedder)


def test_card_text는_제목_본문_태그를_잇는다() -> None:
    card = Card(
        card_id="card_1",
        kind="incident",
        context_id="ctx_inc_1",
        title="결제 장애",
        body="PG 응답 지연",
        tags=["payment", "pg"],
        author="hr",
        confidence="high",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    text = card_text(card)
    assert "결제 장애" in text and "PG 응답 지연" in text and "payment pg" in text
