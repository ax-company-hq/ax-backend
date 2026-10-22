"""회사 기억 카드의 PostgreSQL + pgvector 저장 구현 (#48, MEM-01).

`store.py`의 `CardStore`와 같은 메서드·같은 의미를 갖는 `PgCardStore`와,
검색(#47)이 쓰는 벡터 색인 `PgVectorIndex`를 둔다. 접속 정보는 `MEMORY_PG_DSN`,
벡터 차원은 `MEMORY_EMBED_DIM`(기본 768)에서 읽는다.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ax_contracts.schemas.common import TENANT_ID_DEFAULT
from ax_contracts.schemas.ids import IdPrefix
from ax_contracts.schemas.record import Card
from ax_memory.store import (
    CardDraft,
    CardKind,
    CardStatus,
    WriteResult,
    idempotency_key,
)

if TYPE_CHECKING:
    import psycopg

DEFAULT_EMBED_DIM = 768

# SELECT·RETURNING에서 쓰는 카드 컬럼 순서. 행 -> Card 변환(`row_to_card`)과 맞춰야 한다.
CARD_COLUMNS = (
    "card_id, tenant_id, context_id, kind, title, body, tags, status, "
    "superseded_by, source_refs, author, confidence, created_at, extra"
)


def get_embed_dim() -> int:
    """`MEMORY_EMBED_DIM` 환경변수에서 벡터 차원을 읽는다. 없으면 768."""
    raw = os.environ.get("MEMORY_EMBED_DIM", "").strip()
    if not raw:
        return DEFAULT_EMBED_DIM
    try:
        dim = int(raw)
    except ValueError as exc:
        raise ValueError(f"MEMORY_EMBED_DIM은 정수여야 한다: {raw!r}") from exc
    if dim <= 0:
        raise ValueError(f"MEMORY_EMBED_DIM은 양수여야 한다: {dim}")
    return dim


def get_dsn() -> str:
    """`MEMORY_PG_DSN`을 읽는다. 없으면 명확한 에러를 낸다 (하드코딩 금지)."""
    dsn = os.environ.get("MEMORY_PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("MEMORY_PG_DSN 환경변수가 설정되지 않았다")
    return dsn


def schema_sql(embed_dim: int | None = None) -> str:
    """`cards` 테이블과 pgvector 확장을 만드는 SQL."""
    dim = get_embed_dim() if embed_dim is None else embed_dim
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS cards (
    card_id          text PRIMARY KEY,
    tenant_id        text NOT NULL,
    context_id       text NOT NULL,
    kind             text NOT NULL,
    title            text NOT NULL,
    body             text NOT NULL,
    tags             jsonb NOT NULL DEFAULT '[]'::jsonb,
    status           text NOT NULL DEFAULT 'active',
    superseded_by    text,
    source_refs      jsonb NOT NULL DEFAULT '[]'::jsonb,
    author           text NOT NULL,
    confidence       text NOT NULL,
    created_at       timestamptz NOT NULL,
    extra            jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    idempotency_key  text NOT NULL,
    embedding        vector({dim}),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS cards_tenant_kind_status_idx
    ON cards (tenant_id, kind, status);
"""


def connect(dsn: str | None = None) -> psycopg.Connection:
    """DSN으로 접속한다(autocommit). DSN을 안 주면 `MEMORY_PG_DSN`을 쓴다."""
    import psycopg

    return psycopg.connect(dsn or get_dsn(), autocommit=True)


def ensure_schema(conn: psycopg.Connection, embed_dim: int | None = None) -> None:
    """확장·테이블·인덱스를 만든다. 여러 번 불러도 안전하다."""
    conn.execute(schema_sql(embed_dim))  # type: ignore[arg-type]


def row_to_card(row: tuple[Any, ...]) -> Card:
    """`CARD_COLUMNS` 순서의 행을 `Card`로 바꾼다."""
    (
        card_id,
        tenant_id,
        context_id,
        kind,
        title,
        body,
        tags,
        status,
        superseded_by,
        source_refs,
        author,
        confidence,
        created_at,
        extra,
    ) = row
    return Card(
        card_id=card_id,
        kind=kind,
        tenant_id=tenant_id,
        context_id=context_id,
        title=title,
        body=body,
        tags=list(tags),
        status=status,
        superseded_by=superseded_by,
        source_refs=list(source_refs),
        author=author,
        confidence=confidence,
        created_at=created_at,
        extra=dict(extra),
    )


def card_to_row(card: Card, key: str) -> tuple[Any, ...]:
    """`Card`와 멱등 키를 INSERT용 값 튜플로 바꾼다 (embedding 제외)."""
    return (
        card.card_id,
        card.tenant_id,
        card.context_id,
        card.kind,
        card.title,
        card.body,
        json.dumps(card.tags, ensure_ascii=False),
        card.status,
        card.superseded_by,
        json.dumps(card.source_refs, ensure_ascii=False),
        card.author,
        card.confidence,
        card.created_at,
        json.dumps(card.extra, ensure_ascii=False),
        key,
    )


def _default_id_factory() -> str:
    return f"{IdPrefix.CARD.value}{uuid.uuid4().hex}"


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _vector_literal(vector: list[float]) -> str:
    """pgvector 텍스트 표기 `[a,b,c]`. `::vector`로 캐스팅해서 쓴다."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


class PgCardStore:
    """PostgreSQL에 카드를 저장하는 `CardStore` 구현."""

    def __init__(
        self,
        conn: psycopg.Connection,
        id_factory: Callable[[], str] = _default_id_factory,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._conn = conn
        self._id_factory = id_factory
        self._clock = clock
        self._lock = threading.Lock()

    def write(self, draft: CardDraft) -> WriteResult:
        if not draft.tenant_id or not draft.tenant_id.strip():
            raise ValueError("tenant_id는 비어 있을 수 없다")
        if not draft.context_id or not draft.context_id.strip():
            raise ValueError("context_id는 비어 있을 수 없다")

        key = idempotency_key(draft)
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
        with self._lock:
            inserted = self._conn.execute(
                f"""
                INSERT INTO cards (
                    card_id, tenant_id, context_id, kind, title, body, tags, status,
                    superseded_by, source_refs, author, confidence, created_at, extra,
                    idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s, %s, %s,
                          %s::jsonb, %s)
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING {CARD_COLUMNS}
                """,  # noqa: S608 - CARD_COLUMNS는 상수
                card_to_row(card, key),
            ).fetchone()
            if inserted is not None:
                return WriteResult(card=row_to_card(inserted), created=True)
            existing = self._conn.execute(
                f"SELECT {CARD_COLUMNS} FROM cards "  # noqa: S608
                "WHERE tenant_id = %s AND idempotency_key = %s",
                (draft.tenant_id, key),
            ).fetchone()
            assert existing is not None
            return WriteResult(card=row_to_card(existing), created=False)

    def get(self, card_id: str) -> Card | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {CARD_COLUMNS} FROM cards WHERE card_id = %s",  # noqa: S608
                (card_id,),
            ).fetchone()
        return row_to_card(row) if row is not None else None

    def list(
        self,
        kind: CardKind | None = None,
        status: CardStatus | None = None,
        tenant_id: str = TENANT_ID_DEFAULT,
    ) -> list[Card]:
        sql = f"SELECT {CARD_COLUMNS} FROM cards WHERE tenant_id = %s"  # noqa: S608
        params: list[Any] = [tenant_id]
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at, card_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()  # type: ignore[arg-type]
        return [row_to_card(r) for r in rows]

    def update_status(
        self,
        card_id: str,
        status: CardStatus,
        superseded_by: str | None = None,
    ) -> Card:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {CARD_COLUMNS} FROM cards WHERE card_id = %s",  # noqa: S608
                (card_id,),
            ).fetchone()
            if row is None:
                raise KeyError(card_id)
            # 모델 검증을 다시 태워 superseded/superseded_by 짝을 확인한다.
            updated = Card.model_validate(
                {**row_to_card(row).model_dump(), "status": status, "superseded_by": superseded_by}
            )
            self._conn.execute(
                "UPDATE cards SET status = %s, superseded_by = %s WHERE card_id = %s",
                (updated.status, updated.superseded_by, card_id),
            )
            return updated


class PgVectorIndex:
    """`cards.embedding` 열에 쓰는 벡터 색인. 코사인 유사도로 검색한다."""

    def __init__(self, conn: psycopg.Connection, embed_dim: int | None = None) -> None:
        self._conn = conn
        self._dim = get_embed_dim() if embed_dim is None else embed_dim
        self._lock = threading.Lock()

    def _check_dim(self, vector: list[float]) -> None:
        if len(vector) != self._dim:
            raise ValueError(f"벡터 차원이 {self._dim}이어야 한다: {len(vector)}")

    def upsert(self, card_id: str, tenant_id: str, vector: list[float]) -> None:
        """카드의 임베딩을 저장·갱신한다. 카드가 없으면 KeyError."""
        self._check_dim(vector)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET embedding = %s::vector WHERE card_id = %s AND tenant_id = %s",
                (_vector_literal(vector), card_id, tenant_id),
            )
            if cur.rowcount == 0:
                raise KeyError(card_id)

    def query(
        self,
        tenant_id: str,
        vector: list[float],
        top_k: int,
        allowed_ids: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        """(card_id, 코사인 유사도)를 유사도 내림차순, 같으면 card_id 순으로 돌려준다."""
        self._check_dim(vector)
        if top_k <= 0 or (allowed_ids is not None and not allowed_ids):
            return []
        literal = _vector_literal(vector)
        sql = (
            "SELECT card_id, 1 - (embedding <=> %s::vector) AS score FROM cards "
            "WHERE tenant_id = %s AND embedding IS NOT NULL"
        )
        params: list[Any] = [literal, tenant_id]
        if allowed_ids is not None:
            sql += " AND card_id = ANY(%s)"
            params.append(sorted(allowed_ids))
        sql += " ORDER BY score DESC, card_id LIMIT %s"
        params.append(top_k)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()  # type: ignore[arg-type]
        return [(str(cid), float(score)) for cid, score in rows]
