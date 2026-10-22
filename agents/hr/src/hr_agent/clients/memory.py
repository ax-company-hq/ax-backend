"""회사 기억(memory) 연결: 카드 저장·검색·상태 변경.

실제 연동(3단계)은 같은 `MemoryClient` 인터페이스로 memory 서비스를 부른다. 저장 같은 상태 변경은
호출하는 쪽이 `execute("memory.card_write")`가 `executed`를 돌려준 뒤에만 부른다.

카드의 상태는 계약 `Card.status`(`active`·`superseded`·`retired`)에 "후보"를 더해 다룬다.
후보(`candidate`)는 `status`가 `active`이고 `extra["candidate"]`가 true인 카드다.
계약에 `candidate` 상태가 들어오면(후속 계약 PR) 이 변환만 바꾸면 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ax_contracts.schemas import Card
from hr_agent.config import Clock
from hr_agent.store import Store
from hr_agent.util import dumps, loads, similarity


def card_state(card: Card) -> str:
    """검색·표시에 쓰는 상태: `candidate`·`active`·`superseded`·`retired`."""
    if card.status == "active" and card.extra.get("candidate"):
        return "candidate"
    return card.status


def card_text(card: Card) -> str:
    """유사도 비교에 쓰는 카드 본문(제목·결정·이유·본문·태그)."""
    extra = card.extra
    parts = [card.title, str(extra.get("decision", "")), str(extra.get("reason", "")), card.body]
    parts.append(" ".join(card.tags))
    return " ".join(p for p in parts if p)


@dataclass(frozen=True)
class CardHit:
    card: Card
    score: float


class MemoryClient(Protocol):
    async def write_card(self, card: Card) -> str: ...

    async def get_card(self, card_id: str) -> Card | None: ...

    async def search_cards(
        self,
        query: str,
        *,
        area: str | None = None,
        kinds: list[str] | None = None,
        states: list[str] | None = None,
        limit: int = 5,
    ) -> list[CardHit]: ...

    async def list_cards(
        self, *, area: str | None = None, states: list[str] | None = None
    ) -> list[Card]: ...

    async def set_status(
        self, card_id: str, status: str, superseded_by: str | None = None
    ) -> None: ...

    async def set_candidate(self, card_id: str, candidate: bool) -> None: ...


class MockMemoryClient:
    """SQLite(`mock_cards`)에 카드를 쌓는 목 저장소. 검색은 글자 2-gram 유사도다."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def write_card(self, card: Card) -> str:
        self._store.execute(
            "INSERT OR REPLACE INTO mock_cards(card_id, tenant_id, data, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                card.card_id,
                card.tenant_id,
                dumps(card.model_dump(mode="json")),
                card.created_at.isoformat(),
            ),
        )
        return card.card_id

    async def get_card(self, card_id: str) -> Card | None:
        row = self._store.one("SELECT data FROM mock_cards WHERE card_id = ?", (card_id,))
        return Card.model_validate(loads(row["data"])) if row else None

    async def list_cards(
        self, *, area: str | None = None, states: list[str] | None = None
    ) -> list[Card]:
        rows = self._store.all("SELECT data FROM mock_cards ORDER BY created_at, card_id")
        cards = [Card.model_validate(loads(r["data"])) for r in rows]
        return [c for c in cards if _match(c, area, None, states)]

    async def search_cards(
        self,
        query: str,
        *,
        area: str | None = None,
        kinds: list[str] | None = None,
        states: list[str] | None = None,
        limit: int = 5,
    ) -> list[CardHit]:
        hits = [
            CardHit(card, round(similarity(query, card_text(card)), 4))
            for card in await self.list_cards(area=area, states=states)
            if _match(card, area, kinds, states)
        ]
        hits.sort(key=lambda h: (-h.score, h.card.card_id))
        return hits[:limit]

    async def set_status(self, card_id: str, status: str, superseded_by: str | None = None) -> None:
        card = await self.get_card(card_id)
        if card is None:
            raise KeyError(card_id)
        updated = card.model_copy(update={"status": status, "superseded_by": superseded_by})
        await self.write_card(Card.model_validate(updated.model_dump()))

    async def set_candidate(self, card_id: str, candidate: bool) -> None:
        card = await self.get_card(card_id)
        if card is None:
            raise KeyError(card_id)
        extra = {**card.extra, "candidate": candidate}
        tags = [t for t in card.tags if t != "candidate"] + (["candidate"] if candidate else [])
        await self.write_card(
            Card.model_validate(card.model_copy(update={"extra": extra, "tags": tags}).model_dump())
        )


def _match(card: Card, area: str | None, kinds: list[str] | None, states: list[str] | None) -> bool:
    if area is not None and card.extra.get("area") != area:
        return False
    if kinds is not None and card.kind not in kinds:
        return False
    return not (states is not None and card_state(card) not in states)
