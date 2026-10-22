"""검색 코어 테스트. 네트워크 없이 돈다.

memory 패키지에는 async 테스트 플러그인이 없어 `asyncio.run`으로 돌린다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ax_agent_template.llm import MockLLMClient
from ax_contracts.schemas.record import Card
from ax_memory.embed import LLMEmbedder
from ax_memory.search import InMemoryVectorIndex, SearchHit, index_card, search_cards
from ax_memory.store import CardDraft, InMemoryCardStore

NOW = datetime(2026, 5, 1, tzinfo=UTC)


class _Env:
    def __init__(self) -> None:
        self.clock_value = NOW
        self.store = InMemoryCardStore(clock=lambda: self.clock_value)
        self.index = InMemoryVectorIndex()
        self.embedder = LLMEmbedder(MockLLMClient())

    def add(self, title: str, body: str, **kw: Any) -> Card:
        draft = CardDraft(
            kind=kw.pop("kind", "incident"),
            context_id=kw.pop("context_id", "ctx_inc_1"),
            title=title,
            body=body,
            author="hr",
            confidence=kw.pop("confidence", "high"),
            **kw,
        )
        card = self.store.write(draft).card
        asyncio.run(index_card(self.index, self.embedder, card))
        return card

    def search(self, query: str, **kw: Any) -> list[SearchHit]:
        return asyncio.run(search_cards(self.store, self.index, self.embedder, query, **kw))


@pytest.fixture
def env() -> _Env:
    e = _Env()
    e.add("결제 API 장애", "결제 API 장애로 주문 실패 급증, PG 롤백으로 복구", tags=["payment"])
    e.add(
        "쿠폰 발급 이벤트",
        "신규 회원 쿠폰 발급 이벤트 정책 결정",
        kind="decision",
        context_id="ctx_2",
    )
    e.add("배송 지연 문의", "택배사 배송 지연 고객 문의 응대 사례", kind="case", context_id="ctx_3")
    return e


def test_설명으로_검색하면_결제_장애_카드가_1위다(env: _Env) -> None:
    hits = env.search("결제 API 장애로 주문 실패")
    assert hits[0].card.title == "결제 API 장애"
    assert 0.0 <= hits[0].score <= 1.0
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_superseded_카드는_기본_검색에서_빠진다(env: _Env) -> None:
    old = env.search("결제 API 장애로 주문 실패")[0].card
    new = env.add("결제 재장애", "결제 API 장애 재발", context_id="ctx_new")
    env.store.update_status(old.card_id, "superseded", superseded_by=new.card_id)
    ids = {h.card.card_id for h in env.search("결제 API 장애로 주문 실패")}
    assert old.card_id not in ids and new.card_id in ids
    old_hits = env.search("결제 API 장애로 주문 실패", status="superseded")
    assert [h.card.card_id for h in old_hits] == [old.card_id]


def test_kind_필터(env: _Env) -> None:
    hits = env.search("쿠폰 결제 배송", kind=["decision", "case"], top_k=10)
    assert {h.card.kind for h in hits} == {"decision", "case"}


def test_since_필터(env: _Env) -> None:
    env.clock_value = NOW + timedelta(days=10)
    env.add("최근 결제 장애", "결제 API 장애 최근", context_id="ctx_late")
    hits = env.search("결제 API 장애", since=NOW + timedelta(days=5), top_k=10)
    assert [h.card.title for h in hits] == ["최근 결제 장애"]


def test_tenant가_분리된다(env: _Env) -> None:
    env.add("다른 테넌트 결제 장애", "결제 API 장애", tenant_id="other", context_id="ctx_o")
    shop = env.search("결제 API 장애", top_k=10)
    other = env.search("결제 API 장애", top_k=10, tenant_id="other")
    assert all(h.card.tenant_id == "shop-demo" for h in shop)
    assert [h.card.tenant_id for h in other] == ["other"]


def test_low_confidence는_표시만_하고_제외하지_않는다(env: _Env) -> None:
    env.add("저확신 결제 장애", "결제 API 장애 추정", confidence="low", context_id="ctx_l")
    hits = env.search("결제 API 장애", top_k=10)
    flags = {h.card.title: h.low_confidence for h in hits}
    assert flags["저확신 결제 장애"] is True
    assert flags["결제 API 장애"] is False


@pytest.mark.parametrize("top_k", [0, 21, -1])
def test_top_k_범위를_벗어나면_ValueError(env: _Env, top_k: int) -> None:
    with pytest.raises(ValueError):
        env.search("결제", top_k=top_k)


def test_top_k만큼만_돌려준다(env: _Env) -> None:
    assert len(env.search("결제 쿠폰 배송", top_k=2)) == 2


def test_빈_저장소면_빈_목록() -> None:
    assert _Env().search("결제") == []


def test_같은_점수는_card_id_순이다() -> None:
    index = InMemoryVectorIndex()
    for card_id in ("card_b", "card_a", "card_c"):
        index.upsert(card_id, "shop-demo", [1.0, 0.0])
    result = index.query("shop-demo", [1.0, 0.0], 3)
    assert [c for c, _ in result] == ["card_a", "card_b", "card_c"]
