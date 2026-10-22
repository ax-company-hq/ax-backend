from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ax_memory.store import CardDraft, InMemoryCardStore


def _draft(**overrides: object) -> CardDraft:
    base: dict[str, object] = {
        "kind": "decision",
        "context_id": "ctx_req_1",
        "title": "환불 정책 변경",
        "body": "환불 기한을 7일로 한다",
        "author": "hr",
        "confidence": "high",
    }
    base.update(overrides)
    return CardDraft(**base)  # type: ignore[arg-type]


def _store() -> InMemoryCardStore:
    counter = iter(range(1, 1000))
    return InMemoryCardStore(
        id_factory=lambda: f"card_{next(counter)}",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_write_get_roundtrip() -> None:
    store = _store()
    result = store.write(_draft(tags=["refund"]))
    assert result.created is True
    assert result.card.card_id.startswith("card_")
    assert result.card.status == "active"
    assert result.card.created_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert store.get(result.card.card_id) == result.card
    assert store.get("card_none") is None


def test_default_id_and_clock() -> None:
    card = InMemoryCardStore().write(_draft()).card
    assert card.card_id.startswith("card_")
    assert card.created_at.utcoffset() is not None


def test_same_idempotency_key_returns_existing() -> None:
    store = _store()
    first = store.write(_draft())
    second = store.write(_draft(body="다른 본문"))
    assert second.created is False
    assert second.card == first.card
    assert len(store.list()) == 1


def test_different_title_creates_second_card() -> None:
    store = _store()
    store.write(_draft())
    result = store.write(_draft(title="다른 제목"))
    assert result.created is True
    assert len(store.list()) == 2


@pytest.mark.parametrize("field_name", ["context_id", "tenant_id"])
def test_empty_ids_rejected(field_name: str) -> None:
    with pytest.raises(ValueError):
        _store().write(_draft(**{field_name: ""}))


def test_supersede() -> None:
    store = _store()
    old = store.write(_draft()).card
    new = store.write(_draft(title="새 결정")).card
    updated = store.update_status(old.card_id, "superseded", superseded_by=new.card_id)
    assert updated.status == "superseded"
    assert updated.superseded_by == new.card_id
    assert store.get(old.card_id) == updated


def test_superseded_without_target_fails_and_keeps_card() -> None:
    store = _store()
    card = store.write(_draft()).card
    with pytest.raises(ValidationError):
        store.update_status(card.card_id, "superseded")
    assert store.get(card.card_id).status == "active"  # type: ignore[union-attr]


def test_update_status_unknown_card() -> None:
    with pytest.raises(KeyError):
        _store().update_status("card_none", "retired")


def test_list_filters() -> None:
    store = _store()
    a = store.write(_draft(kind="incident", title="a")).card
    store.write(_draft(kind="decision", title="b"))
    store.update_status(a.card_id, "retired")
    assert [c.title for c in store.list(kind="incident")] == ["a"]
    assert [c.title for c in store.list(status="retired")] == ["a"]
    assert [c.title for c in store.list(status="active")] == ["b"]
    assert store.list(kind="policy") == []


def test_tenant_separation() -> None:
    store = _store()
    store.write(_draft(tenant_id="shop-demo"))
    other = store.write(_draft(tenant_id="other"))
    assert other.created is True
    assert len(store.list()) == 1
    assert len(store.list(tenant_id="other")) == 1
