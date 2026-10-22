"""PgCardStore·PgVectorIndex 테스트.

DB 없이 도는 테스트(스키마 SQL, 환경변수, 행 변환)와, `MEMORY_PG_DSN`이 있을 때만
도는 DB 테스트로 나뉜다. CI에는 DB가 없어 후자는 skip된다.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from ax_contracts.schemas.record import Card
from ax_memory import pg_store
from ax_memory.pg_store import (
    PgCardStore,
    PgVectorIndex,
    card_to_row,
    connect,
    ensure_schema,
    get_dsn,
    get_embed_dim,
    row_to_card,
    schema_sql,
)
from ax_memory.store import CardDraft, idempotency_key


def _draft(**kw) -> CardDraft:
    base = dict(
        kind="incident",
        context_id="ctx_inc_1",
        title="결제 오류",
        body="본문",
        author="hr",
        confidence="high",
        tags=["pay"],
        source_refs=["evt_1"],
        extra={"k": 1},
    )
    base.update(kw)
    return CardDraft(**base)


# ---------- DB 없이 ----------


def test_schema_sql_has_required_columns():
    sql = schema_sql(768)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "tenant_id        text NOT NULL" in sql
    assert "context_id       text NOT NULL" in sql
    assert "vector(768)" in sql
    assert "UNIQUE (tenant_id, idempotency_key)" in sql


def test_embed_dim_env(monkeypatch):
    monkeypatch.delenv("MEMORY_EMBED_DIM", raising=False)
    assert get_embed_dim() == 768
    monkeypatch.setenv("MEMORY_EMBED_DIM", "1536")
    assert get_embed_dim() == 1536
    assert "vector(1536)" in schema_sql()
    monkeypatch.setenv("MEMORY_EMBED_DIM", "abc")
    with pytest.raises(ValueError):
        get_embed_dim()


def test_missing_dsn_raises_clear_error(monkeypatch):
    monkeypatch.delenv("MEMORY_PG_DSN", raising=False)
    with pytest.raises(RuntimeError, match="MEMORY_PG_DSN"):
        get_dsn()
    with pytest.raises(RuntimeError, match="MEMORY_PG_DSN"):
        connect()


def test_row_card_roundtrip():
    card = Card(
        card_id="card_1",
        kind="decision",
        tenant_id="shop-demo",
        context_id="ctx_req_1",
        title="t",
        body="b",
        tags=["a", "한글"],
        status="superseded",
        superseded_by="card_2",
        source_refs=["x"],
        author="hr",
        confidence="mid",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        extra={"n": 1},
    )
    row = card_to_row(card, "key")
    assert row[-1] == "key"
    # INSERT용 값(json 문자열)을 DB가 돌려주는 값(파이썬 객체)으로 바꿔 왕복 확인
    import json

    db_row = (
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        json.loads(row[6]),
        row[7],
        row[8],
        json.loads(row[9]),
        row[10],
        row[11],
        row[12],
        json.loads(row[13]),
    )
    assert row_to_card(db_row) == card


def test_vector_index_rejects_wrong_dim_without_db():
    index = PgVectorIndex(conn=None, embed_dim=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        index.upsert("card_1", "shop-demo", [0.1, 0.2])
    with pytest.raises(ValueError):
        index.query("shop-demo", [0.1], 3)
    assert index.query("shop-demo", [0.1, 0.2, 0.3], 0) == []
    assert index.query("shop-demo", [0.1, 0.2, 0.3], 3, allowed_ids=set()) == []


# ---------- DB 필요 ----------

DSN = os.environ.get("MEMORY_PG_DSN")
needs_db = pytest.mark.skipif(not DSN, reason="MEMORY_PG_DSN 없음 (DB 테스트 건너뜀)")


@pytest.fixture
def conn():
    c = connect(DSN)
    c.execute("DROP TABLE IF EXISTS cards")
    ensure_schema(c, embed_dim=3)
    yield c
    c.execute("DROP TABLE IF EXISTS cards")
    c.close()


@needs_db
def test_write_get_list_idempotent(conn):
    store = PgCardStore(conn)
    r1 = store.write(_draft())
    assert r1.created
    r2 = store.write(_draft(body="다른 본문"))
    assert not r2.created
    assert r2.card.card_id == r1.card.card_id
    assert store.get(r1.card.card_id) == r1.card
    assert store.get("card_none") is None
    assert store.list(kind="incident") == [r1.card]
    assert store.list(kind="policy") == []
    assert idempotency_key(_draft())  # 재사용 확인


@needs_db
def test_write_rejects_blank(conn):
    store = PgCardStore(conn)
    with pytest.raises(ValueError):
        store.write(_draft(tenant_id=" "))
    with pytest.raises(ValueError):
        store.write(_draft(context_id=""))


@needs_db
def test_update_status(conn):
    store = PgCardStore(conn)
    card = store.write(_draft()).card
    with pytest.raises(KeyError):
        store.update_status("card_none", "retired")
    with pytest.raises(ValueError):
        store.update_status(card.card_id, "superseded")
    new = store.write(_draft(title="새 결정", kind="decision")).card
    updated = store.update_status(card.card_id, "superseded", new.card_id)
    assert updated.status == "superseded"
    assert store.get(card.card_id).superseded_by == new.card_id
    assert store.list(status="active") == [new]


@needs_db
def test_vector_query(conn):
    store = PgCardStore(conn)
    a = store.write(_draft(title="a")).card
    b = store.write(_draft(title="b")).card
    c = store.write(_draft(title="c")).card
    index = PgVectorIndex(conn, embed_dim=3)
    index.upsert(a.card_id, "shop-demo", [1, 0, 0])
    index.upsert(b.card_id, "shop-demo", [0.9, 0.1, 0])
    index.upsert(c.card_id, "shop-demo", [0, 1, 0])
    res = index.query("shop-demo", [1, 0, 0], 2)
    assert [cid for cid, _ in res] == [a.card_id, b.card_id]
    assert res[0][1] == pytest.approx(1.0)
    only_c = index.query("shop-demo", [1, 0, 0], 5, allowed_ids={c.card_id})
    assert [cid for cid, _ in only_c] == [c.card_id]
    assert index.query("other-tenant", [1, 0, 0], 5) == []
    with pytest.raises(KeyError):
        index.upsert("card_none", "shop-demo", [1, 0, 0])


def test_module_exports():
    assert hasattr(pg_store, "PgCardStore")
