import pytest
from pydantic import ValidationError

from ax_contracts.schemas import (
    CardDraft,
    CardHit,
    CardLinkInput,
    CardSearchInput,
    CardWriteInput,
    CardWriteOutput,
)

DRAFT = {
    "kind": "incident",
    "context_id": "ctx_inc_0001",
    "title": "결제 API 장애",
    "body": "롤백으로 복구",
    "author": "hr",
    "confidence": "high",
}

CARD = {
    **DRAFT,
    "card_id": "card_1",
    "created_at": "2026-10-22T06:00:00Z",
}


def test_write_and_search_defaults() -> None:
    write = CardWriteInput.model_validate({"card": DRAFT})
    assert write.card.tenant_id == "shop-demo"
    assert CardWriteOutput(card_id="card_1", created=True).created is True
    search = CardSearchInput(query="결제 장애")
    assert (search.top_k, search.status, search.kind, search.since) == (5, "active", [], None)


def test_top_k_out_of_range_rejected() -> None:
    for top_k in (0, 21):
        with pytest.raises(ValidationError):
            CardSearchInput(query="q", top_k=top_k)
    assert CardSearchInput(query="q", top_k=20).top_k == 20


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        CardSearchInput.model_validate({"query": "q", "limit": 3})
    with pytest.raises(ValidationError):
        CardDraft.model_validate({**DRAFT, "topK": 1})


def test_draft_rejects_server_fields() -> None:
    for field, value in (
        ("card_id", "card_1"),
        ("created_at", "2026-10-22T06:00:00Z"),
        ("status", "active"),
        ("superseded_by", "card_2"),
    ):
        with pytest.raises(ValidationError):
            CardDraft.model_validate({**DRAFT, field: value})


def test_card_id_prefix_and_score_range() -> None:
    with pytest.raises(ValidationError):
        CardWriteOutput(card_id="abc", created=True)
    with pytest.raises(ValidationError):
        CardLinkInput(old_card_id="card_1", new_card_id="card_2", relation="deleted")
    assert CardHit.model_validate({"card": CARD, "score": 0.5, "low_confidence": False})
    with pytest.raises(ValidationError):
        CardHit.model_validate({"card": CARD, "score": 1.5, "low_confidence": False})
