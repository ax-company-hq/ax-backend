"""판매 사이트 저장소(SQLite). 테이블은 복수형 snake_case이고 모두 `tenant_id`·`context_id`를 둔다.

`SHOP_DB_PATH`(기본 `:memory:`)로 파일을 지정한다. 연결 하나를 잠금으로 보호한다(데모 규모).
시간은 UTC ISO8601 문자열, 금액은 정수 원이다.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from ax_contracts.schemas.common import TENANT_ID_DEFAULT

SCHEMA = """
CREATE TABLE IF NOT EXISTS sellers (
  seller_id TEXT PRIMARY KEY, name TEXT NOT NULL, category_codes TEXT NOT NULL,
  tenant_id TEXT NOT NULL, context_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
  product_id TEXT PRIMARY KEY, seller_id TEXT NOT NULL REFERENCES sellers(seller_id),
  name TEXT NOT NULL, category_code TEXT NOT NULL, price INTEGER NOT NULL CHECK (price >= 0),
  stock INTEGER NOT NULL CHECK (stock >= 0), active INTEGER NOT NULL DEFAULT 1,
  tenant_id TEXT NOT NULL, context_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS buyers (
  buyer_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, context_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL REFERENCES buyers(buyer_id),
  status TEXT NOT NULL, total_amount INTEGER NOT NULL, tenant_id TEXT NOT NULL,
  context_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
  order_id TEXT NOT NULL REFERENCES orders(order_id), product_id TEXT NOT NULL,
  seller_id TEXT NOT NULL, qty INTEGER NOT NULL CHECK (qty > 0),
  unit_price INTEGER NOT NULL, tenant_id TEXT NOT NULL, context_id TEXT
);
CREATE TABLE IF NOT EXISTS payments (
  payment_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(order_id),
  amount INTEGER NOT NULL, method_code TEXT NOT NULL, status TEXT NOT NULL,
  failure_code TEXT, tenant_id TEXT NOT NULL, context_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_events (
  event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, tenant_id TEXT NOT NULL,
  context_id TEXT, created_at TEXT NOT NULL, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS banners (
  notice_id TEXT PRIMARY KEY, text TEXT NOT NULL, status TEXT NOT NULL,
  valid_from TEXT, valid_to TEXT, tenant_id TEXT NOT NULL, context_id TEXT,
  created_at TEXT NOT NULL, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS coupons (
  coupon_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL REFERENCES buyers(buyer_id),
  purpose_code TEXT NOT NULL, campaign_id TEXT, count INTEGER NOT NULL CHECK (count > 0),
  unit_value INTEGER NOT NULL CHECK (unit_value > 0), total_value INTEGER NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, tenant_id TEXT NOT NULL, context_id TEXT,
  created_at TEXT NOT NULL
);
"""

TENANT_ID = TENANT_ID_DEFAULT


class Db:
    def __init__(self, path: str | None = None) -> None:
        self._conn = sqlite3.connect(
            path or os.getenv("SHOP_DB_PATH", ":memory:"), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """한 트랜잭션. 예외가 나면 되돌린다(주문·결제·outbox가 함께 저장되거나 함께 취소된다)."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def next_id(self, conn: sqlite3.Connection, name: str, prefix: str) -> str:
        """`prefix + 4자리 이상 순번`. 접두사 규칙은 CLAUDE.md ID 표를 따른다."""
        conn.execute(
            "INSERT INTO counters(name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1",
            (name,),
        )
        value = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0]
        return f"{prefix}{value:04d}"
