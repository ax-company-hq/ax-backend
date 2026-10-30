"""문의·클레임·보상 번호의 영속 저장 (PostgreSQL).

`SUPPORT_PG_DSN`이 있으면 메모리 저장소(`InquiryStore`, `ClaimStore`, `SequenceLedger`)를 그대로
상속한 영속 저장소를 쓴다. 읽기는 메모리 캐시, 쓰기는 같은 호출 안에서 DB에 반영한다(write-through).
시작할 때 DB에서 전부 읽어 오므로 재시작해도 문의·클레임·멱등 결과·보상 `sequence`가 이어진다.
DSN이 없으면(CI, 목 모드) 예전처럼 순수 메모리 저장소를 쓴다.

- 테이블은 복수형 snake_case이고 모두 `tenant_id`·`context_id`를 갖는다. 본문은 `doc`(jsonb)에
  모델을 그대로 담고, 조회·갱신에 쓰는 값만 별도 컬럼으로 둔다.
- 쓰기가 실패하면 예외를 그대로 올린다 (삼키지 않는다). 안전 레이어가 이미 허가한 뒤라면 같은
  `idempotency_key`로 다시 요청해야 하지만, 메모리에는 이미 반영돼 있을 수 있다. 단일 인스턴스를
  가정한다 (여러 프로세스가 같은 DB를 쓰면 캐시가 어긋난다).
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ax_contracts.schemas.common import TENANT_ID_DEFAULT
from support_agent.change_requests import ChangeRequestLink, ChangeRequestStore
from support_agent.claim_schemas import Claim
from support_agent.claims import ClaimStore
from support_agent.compensation import SequenceLedger
from support_agent.schemas import Conversation
from support_agent.store import InquiryStore

logger = logging.getLogger("ax.support.persistence")

DSN_ENV = "SUPPORT_PG_DSN"


@dataclass(frozen=True)
class Table:
    name: str
    pk: tuple[str, ...]
    columns: tuple[str, ...] = ()  # pk 말고 따로 조회·갱신하는 보조 컬럼


TABLES: tuple[Table, ...] = (
    Table("inquiries", ("inquiry_id",), ("tenant_id", "context_id", "status", "updated_at")),
    Table("inquiry_results", ("tenant_id", "idempotency_key"), ("context_id",)),
    Table("draft_events", ("event_id",), ("tenant_id", "context_id", "inquiry_id")),
    Table("privacy_audits", ("audit_id",), ("tenant_id", "context_id", "inquiry_id")),
    Table("claims", ("claim_id",), ("tenant_id", "context_id", "order_id", "buyer_id", "status")),
    Table("change_requests", ("tenant_id", "context_id", "inquiry_id"), ("status",)),
    Table("compensation_sequences", ("tenant_id", "context_id")),
    Table("compensation_results", ("tenant_id", "idempotency_key"), ("context_id",)),
)
BY_NAME = {t.name: t for t in TABLES}

COLUMN_TYPES = {
    "inquiry_id": "bigint",
    "event_id": "bigint",
    "audit_id": "bigint",
    "claim_id": "bigint",
    "updated_at": "timestamptz",
}


def column_type(column: str) -> str:
    return COLUMN_TYPES.get(column, "text")


# 주문·구매자 ID가 숫자였던 예전 `claims` 테이블을 문자열 열로 옮긴다 (`user_id` → `buyer_id`,
# 값은 `buy_<4자리>`). 잠시 쓰던 `usr_<번호>`도 같은 형식으로 고친다.
# 새로 만든 테이블에는 아무 일도 하지 않고, 여러 번 실행해도 안전하다. 예전 기록의 `doc`은
# `Claim` 모델이 읽을 때 문자열로 바꿔 준다 (`claim_schemas.EntityId`).
LEGACY_CLAIMS_MIGRATION = """DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
             AND table_name = 'claims' AND column_name = 'user_id') THEN
    ALTER TABLE claims RENAME COLUMN user_id TO buyer_id;
    ALTER TABLE claims ALTER COLUMN buyer_id TYPE text
      USING 'buy_' || lpad(buyer_id::text, 4, '0');
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
             AND table_name = 'claims' AND column_name = 'order_id'
             AND data_type = 'bigint') THEN
    ALTER TABLE claims ALTER COLUMN order_id TYPE text USING order_id::text;
  END IF;
  UPDATE claims SET buyer_id = 'buy_' || lpad(substr(buyer_id, 5), 4, '0')
    WHERE buyer_id ~ '^usr_[0-9]+$';
END $$;"""


def schema_sql() -> str:
    """테이블을 만드는 SQL. 여러 번 실행해도 안전하다."""
    statements = []
    for table in TABLES:
        columns = [f"    {c} {column_type(c)} NOT NULL" for c in (*table.pk, *table.columns)]
        columns.append("    doc jsonb NOT NULL")
        columns.append(f"    PRIMARY KEY ({', '.join(table.pk)})")
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {table.name} (\n" + ",\n".join(columns) + "\n);"
        )
    statements.append(LEGACY_CLAIMS_MIGRATION)
    statements.append("CREATE INDEX IF NOT EXISTS claims_status_idx ON claims (tenant_id, status);")
    statements.append(
        "CREATE INDEX IF NOT EXISTS inquiries_status_idx ON inquiries (tenant_id, status);"
    )
    return "\n".join(statements)


class Repo(Protocol):
    """저장소가 쓰는 최소 인터페이스. 테스트는 같은 모양의 메모리 구현으로 바꾼다."""

    def load(self, table: str) -> list[dict[str, Any]]:
        """테이블의 `doc`을 기본키 순서로 모두 돌려준다."""
        ...

    def upsert(self, table: str, values: dict[str, Any], doc: dict[str, Any]) -> None:
        """기본키·보조 컬럼 값(`values`)과 `doc`을 넣거나 덮어쓴다."""
        ...


def _upsert_sql(table: Table) -> str:
    columns = [*table.pk, *table.columns, "doc"]
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in (*table.columns, "doc"))
    return (
        f"INSERT INTO {table.name} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(table.pk)}) DO UPDATE SET {updates}"
    )


class PgRepo:
    """psycopg로 PostgreSQL에 쓰고 읽는다. 연결이 끊기면 한 번 다시 연결해 재시도한다."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._lock = threading.RLock()
        self._conn: Any = None
        self._upserts = {t.name: _upsert_sql(t) for t in TABLES}
        self._run(lambda conn: conn.execute(schema_sql()))  # type: ignore[arg-type]

    def _connect(self) -> Any:
        import psycopg

        return psycopg.connect(self._dsn, autocommit=True)

    def _run(self, action: Callable[[Any], Any]) -> Any:
        import psycopg

        with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = self._connect()
            try:
                return action(self._conn)
            except psycopg.OperationalError:
                logger.warning("DB 연결이 끊겨 다시 연결한다", exc_info=True)
                self._conn = self._connect()
                return action(self._conn)

    def load(self, table: str) -> list[dict[str, Any]]:
        spec = BY_NAME[table]
        sql = f"SELECT doc FROM {spec.name} ORDER BY {', '.join(spec.pk)}"
        rows = self._run(lambda conn: conn.execute(sql).fetchall())
        return [dict(row[0]) for row in rows]

    def upsert(self, table: str, values: dict[str, Any], doc: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        spec = BY_NAME[table]
        params = [values[c] for c in (*spec.pk, *spec.columns)] + [Jsonb(doc)]
        self._run(lambda conn: conn.execute(self._upserts[table], params))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()


# ---------------------------------------------------------------- 쓰기를 DB로 흘려 보내는 컬렉션
def _context_of(key: str) -> str:
    """멱등 키는 `<context_id>:<액션>:<대상>` 형식이다. 앞 토막을 사건 ID로 쓴다."""
    return key.split(":", 1)[0]


class PersistentDict(dict):
    """`results[key] = output`이 DB에도 저장되는 dict (멱등 결과). `doc`에 키와 결과를 함께 둔다."""

    def __init__(self, repo: Repo, table: str) -> None:
        super().__init__({doc["key"]: doc["result"] for doc in repo.load(table)})
        self._repo = repo
        self._table = table

    def __setitem__(self, key: str, value: dict) -> None:
        self._repo.upsert(
            self._table,
            {
                "tenant_id": TENANT_ID_DEFAULT,
                "idempotency_key": key,
                "context_id": _context_of(key),
            },
            {"key": key, "result": value},
        )
        super().__setitem__(key, value)


class PersistentList(list):
    """`events.append(event)`가 DB에도 저장되는 list (초안 채택·수정·거절 기록)."""

    def __init__(self, repo: Repo, initial: list[dict] | None = None) -> None:
        super().__init__(initial or [])
        self._repo = repo

    def append(self, event: dict) -> None:
        self._repo.upsert(
            "draft_events",
            {
                "event_id": len(self) + 1,
                "tenant_id": TENANT_ID_DEFAULT,
                "context_id": str(event.get("context_id") or ""),
                "inquiry_id": int(event["inquiry_id"]),
            },
            event,
        )
        super().append(event)


class PersistentAuditList(list):
    """`audit.append(record)`가 DB에도 저장되는 list (개인정보 감사 기록, 추가만 한다)."""

    def __init__(self, repo: Repo, initial: list[dict] | None = None) -> None:
        super().__init__(initial or [])
        self._repo = repo

    def append(self, record: dict) -> None:
        self._repo.upsert(
            "privacy_audits",
            {
                "audit_id": int(record["audit_id"]),
                "tenant_id": str(record.get("tenant_id") or TENANT_ID_DEFAULT),
                "context_id": str(record.get("context_id") or ""),
                "inquiry_id": int(record.get("inquiry_id") or 0),  # 문의와 무관한 기록은 0
            },
            record,
        )
        super().append(record)


# ---------------------------------------------------------------- 영속 저장소
class PersistentInquiryStore(InquiryStore):
    def __init__(self, repo: Repo) -> None:
        super().__init__()
        self._repo = repo
        for doc in repo.load("inquiries"):
            conversation = Conversation.model_validate(doc)
            self._conversations[conversation.id] = conversation
        self._inquiry_ids = itertools.count(max(self._conversations, default=0) + 1)
        last_message = max(
            (m.id for c in self._conversations.values() for m in c.messages), default=0
        )
        self._message_ids = itertools.count(last_message + 1)
        self.results = PersistentDict(repo, "inquiry_results")
        self.draft_events = PersistentList(repo, repo.load("draft_events"))
        self.privacy_audit = PersistentAuditList(repo, repo.load("privacy_audits"))
        self._audit_ids = itertools.count(
            max((r["audit_id"] for r in self.privacy_audit), default=0) + 1
        )

    def _save(self, conversation: Conversation) -> Conversation:
        self._repo.upsert(
            "inquiries",
            {
                "inquiry_id": conversation.id,
                "tenant_id": conversation.tenant_id,
                "context_id": conversation.context_id,
                "status": conversation.status,
                "updated_at": conversation.updated_at,
            },
            conversation.model_dump(mode="json"),
        )
        return conversation

    def create(self, **kwargs: Any) -> Conversation:  # type: ignore[override]
        return self._save(super().create(**kwargs))

    def add_customer_message(self, *args: Any, **kwargs: Any) -> Conversation:
        return self._save(super().add_customer_message(*args, **kwargs))

    def add_agent_message(self, *args: Any, **kwargs: Any) -> Conversation:
        return self._save(super().add_agent_message(*args, **kwargs))

    def set_status(self, *args: Any, **kwargs: Any) -> Conversation:
        return self._save(super().set_status(*args, **kwargs))


class PersistentClaimStore(ClaimStore):
    def __init__(self, repo: Repo) -> None:
        super().__init__()
        self._repo = repo
        for doc in repo.load("claims"):
            claim = Claim.model_validate(doc)
            self._claims[claim.id] = claim
        self._ids = itertools.count(max(self._claims, default=0) + 1)

    def _save(self, claim: Claim) -> Claim:
        self._repo.upsert(
            "claims",
            {
                "claim_id": claim.id,
                "tenant_id": claim.tenant_id,
                "context_id": claim.context_id,
                "order_id": claim.order_id,
                "buyer_id": claim.buyer_id,
                "status": claim.status,
            },
            claim.model_dump(mode="json"),
        )
        return claim

    def create(self, **kwargs: Any) -> Claim:  # type: ignore[override]
        return self._save(super().create(**kwargs))

    def set_status(self, claim: Claim, status: str, action: str, **detail: Any) -> Claim:
        # 호출한 쪽이 먼저 바꿔 둔 필드(메모·검수·지급 기록)도 이때 함께 저장된다
        return self._save(super().set_status(claim, status, action, **detail))


class PersistentChangeRequestStore(ChangeRequestStore):
    """문의와 기획개발 변경 요청의 연결을 DB에 둔다. `_put` 한 곳에서만 쓰면 된다."""

    def __init__(self, repo: Repo) -> None:
        super().__init__()
        self._repo = repo
        for doc in repo.load("change_requests"):
            link = ChangeRequestLink.model_validate(doc)
            self._links[(link.context_id, link.inquiry_id)] = link

    def _put(self, link: ChangeRequestLink) -> ChangeRequestLink:
        self._repo.upsert(
            "change_requests",
            {
                "tenant_id": link.tenant_id,
                "context_id": link.context_id,
                "inquiry_id": link.inquiry_id,
                "status": link.status,
            },
            link.model_dump(mode="json"),
        )
        return super()._put(link)  # DB 쓰기에 성공한 뒤에 캐시에 넣는다


class PersistentSequenceLedger(SequenceLedger):
    def __init__(self, repo: Repo) -> None:
        super().__init__()
        self._repo = repo
        for doc in repo.load("compensation_sequences"):
            self._next[doc["context_id"]] = int(doc["next_sequence"])
        self.results = PersistentDict(repo, "compensation_results")

    def advance(self, context_id: str) -> None:
        super().advance(context_id)
        self._repo.upsert(
            "compensation_sequences",
            {"tenant_id": TENANT_ID_DEFAULT, "context_id": context_id},
            {"context_id": context_id, "next_sequence": self._next[context_id]},
        )


# ---------------------------------------------------------------- 선택
_repos: dict[str, Repo] = {}  # DSN별로 연결을 한 번만 만든다


def dsn() -> str | None:
    return os.environ.get(DSN_ENV, "").strip() or None


def get_repo() -> Repo | None:
    """`SUPPORT_PG_DSN`이 있으면 DB 저장소를, 없으면 None(메모리 사용)을 돌려준다."""
    value = dsn()
    if value is None:
        return None
    if value not in _repos:
        _repos[value] = PgRepo(value)
    return _repos[value]


def build_inquiry_store() -> InquiryStore:
    repo = get_repo()
    return PersistentInquiryStore(repo) if repo else InquiryStore()


def build_claim_store() -> ClaimStore:
    repo = get_repo()
    return PersistentClaimStore(repo) if repo else ClaimStore()


def build_sequence_ledger() -> SequenceLedger:
    repo = get_repo()
    return PersistentSequenceLedger(repo) if repo else SequenceLedger()


def build_change_request_store() -> ChangeRequestStore:
    repo = get_repo()
    return PersistentChangeRequestStore(repo) if repo else ChangeRequestStore()
