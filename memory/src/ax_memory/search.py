"""카드 벡터 인덱스와 검색 코어 (#47, MEM-02).

`VectorIndex`는 pgvector 구현과 같은 인터페이스다. 여기서는 메모리 구현을 둔다.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ax_contracts.schemas.common import TENANT_ID_DEFAULT
from ax_contracts.schemas.record import Card
from ax_memory.embed import Embedder, card_text
from ax_memory.store import CardStore

TOP_K_MIN = 1
TOP_K_MAX = 20


class VectorIndex(Protocol):
    """카드 벡터 인덱스 인터페이스."""

    def upsert(self, card_id: str, tenant_id: str, vector: list[float]) -> None: ...

    def query(
        self,
        tenant_id: str,
        vector: list[float],
        top_k: int,
        allowed_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """(card_id, 코사인 유사도)를 유사도 내림차순으로 돌려준다."""
        ...


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"벡터 차원이 다르다: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class InMemoryVectorIndex:
    """프로세스 메모리 벡터 인덱스. 테스트와 로컬 실행용이다."""

    def __init__(self) -> None:
        self._vectors: dict[str, tuple[str, list[float]]] = {}  # card_id -> (tenant_id, 벡터)
        self._lock = threading.Lock()

    def upsert(self, card_id: str, tenant_id: str, vector: list[float]) -> None:
        with self._lock:
            self._vectors[card_id] = (tenant_id, list(vector))

    def query(
        self,
        tenant_id: str,
        vector: list[float],
        top_k: int,
        allowed_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        with self._lock:
            items = list(self._vectors.items())
        scored = [
            (card_id, _cosine(vector, stored))
            for card_id, (tenant, stored) in items
            if tenant == tenant_id and (allowed_ids is None or card_id in allowed_ids)
        ]
        # 같은 점수는 card_id 순으로 결정적으로 정렬한다.
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:top_k]


async def index_card(index: VectorIndex, embedder: Embedder, card: Card) -> None:
    """카드 문장을 임베딩해 인덱스에 넣는다(이미 있으면 덮어쓴다)."""
    [vector] = await embedder.embed([card_text(card)])
    index.upsert(card.card_id, card.tenant_id, vector)


@dataclass(frozen=True)
class SearchHit:
    """검색 결과 1건. `low_confidence`는 확신도가 낮은 카드의 표시일 뿐 제외 사유가 아니다."""

    card: Card
    score: float
    low_confidence: bool


async def search_cards(
    store: CardStore,
    index: VectorIndex,
    embedder: Embedder,
    query: str,
    *,
    kind: list[str] | None = None,
    status: str = "active",
    since: datetime | None = None,
    top_k: int = 5,
    tenant_id: str = TENANT_ID_DEFAULT,
) -> list[SearchHit]:
    """필터로 후보를 고른 뒤 그 ID 안에서만 벡터 검색한다."""
    if not TOP_K_MIN <= top_k <= TOP_K_MAX:
        raise ValueError(f"top_k는 {TOP_K_MIN}~{TOP_K_MAX}여야 한다: {top_k}")

    candidates = {
        card.card_id: card
        for card in store.list(status=status, tenant_id=tenant_id)  # type: ignore[arg-type]
        if (kind is None or card.kind in kind) and (since is None or card.created_at >= since)
    }
    if not candidates:
        return []

    [vector] = await embedder.embed([query])
    ranked = index.query(tenant_id, vector, top_k, allowed_ids=set(candidates))
    return [
        SearchHit(
            card=candidates[card_id],
            score=min(1.0, max(0.0, score)),
            low_confidence=candidates[card_id].confidence == "low",
        )
        for card_id, score in ranked
        if card_id in candidates
    ]
