"""회사 기억 카드 저장소 인터페이스와 메모리 구현 (#43).

pgvector 구현(#48)과 검색(#47)이 이 인터페이스를 쓴다. 검색은 여기 범위 밖이다.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from ax_contracts.schemas.common import TENANT_ID_DEFAULT
from ax_contracts.schemas.ids import IdPrefix
from ax_contracts.schemas.record import Card

CardKind = Literal["incident", "decision", "case", "notice", "policy", "fraud"]
CardStatus = Literal["active", "superseded", "retired"]
Confidence = Literal["low", "mid", "high"]


# 계약 #38 머지 후 계약 모델로 교체 예정. 지금은 저장소 내부용 초안이다.
@dataclass(frozen=True)
class CardDraft:
    """저장 전 카드 초안. `Card`에서 card_id·created_at·status·superseded_by를 뺀 것."""

    kind: CardKind
    context_id: str
    title: str
    body: str
    author: str
    confidence: Confidence
    tenant_id: str = TENANT_ID_DEFAULT
    tags: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WriteResult:
    """`write` 결과. 멱등 키가 겹쳐 기존 카드를 돌려줬으면 `created`가 False다."""

    card: Card
    created: bool


def idempotency_key(draft: CardDraft) -> str:
    """멱등 키 = `context_id + kind + sha256(title)`."""
    title_hash = hashlib.sha256(draft.title.encode("utf-8")).hexdigest()
    return f"{draft.context_id}:{draft.kind}:{title_hash}"


class CardStore(Protocol):
    """카드 저장소 인터페이스."""

    def write(self, draft: CardDraft) -> WriteResult: ...

    def get(self, card_id: str) -> Card | None: ...

    def list(
        self,
        kind: CardKind | None = None,
        status: CardStatus | None = None,
        tenant_id: str = TENANT_ID_DEFAULT,
    ) -> list[Card]: ...

    def update_status(
        self,
        card_id: str,
        status: CardStatus,
        superseded_by: str | None = None,
    ) -> Card: ...


def _default_id_factory() -> str:
    return f"{IdPrefix.CARD.value}{uuid.uuid4().hex}"


def _default_clock() -> datetime:
    return datetime.now(UTC)


class InMemoryCardStore:
    """프로세스 메모리에 카드를 두는 구현. 테스트와 로컬 실행용이다."""

    def __init__(
        self,
        id_factory: Callable[[], str] = _default_id_factory,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._id_factory = id_factory
        self._clock = clock
        self._cards: dict[str, Card] = {}
        self._keys: dict[tuple[str, str], str] = {}  # (tenant_id, 멱등 키) -> card_id
        self._lock = threading.Lock()

    def write(self, draft: CardDraft) -> WriteResult:
        if not draft.tenant_id or not draft.tenant_id.strip():
            raise ValueError("tenant_id는 비어 있을 수 없다")
        if not draft.context_id or not draft.context_id.strip():
            raise ValueError("context_id는 비어 있을 수 없다")

        key = (draft.tenant_id, idempotency_key(draft))
        with self._lock:
            existing_id = self._keys.get(key)
            if existing_id is not None:
                return WriteResult(card=self._cards[existing_id], created=False)

            card = Card(
                card_id=self._id_factory(),
                kind=draft.kind,
                tenant_id=draft.tenant_id,
                context_id=draft.context_id,
                title=draft.title,
                body=draft.body,
                tags=list(draft.tags),
                source_refs=list(draft.source_refs),
                author=draft.author,
                confidence=draft.confidence,
                created_at=self._clock(),
                extra=dict(draft.extra),
            )
            self._cards[card.card_id] = card
            self._keys[key] = card.card_id
            return WriteResult(card=card, created=True)

    def get(self, card_id: str) -> Card | None:
        with self._lock:
            return self._cards.get(card_id)

    def list(
        self,
        kind: CardKind | None = None,
        status: CardStatus | None = None,
        tenant_id: str = TENANT_ID_DEFAULT,
    ) -> list[Card]:
        with self._lock:
            return [
                card
                for card in self._cards.values()
                if card.tenant_id == tenant_id
                and (kind is None or card.kind == kind)
                and (status is None or card.status == status)
            ]

    def update_status(
        self,
        card_id: str,
        status: CardStatus,
        superseded_by: str | None = None,
    ) -> Card:
        with self._lock:
            current = self._cards.get(card_id)
            if current is None:
                raise KeyError(card_id)
            # 모델 검증을 다시 태워 superseded/superseded_by 짝을 확인한다.
            updated = Card.model_validate(
                {**current.model_dump(), "status": status, "superseded_by": superseded_by}
            )
            self._cards[card_id] = updated
            return updated
