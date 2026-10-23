"""hr이 소유하는 데이터(SQLite). 테이블 작성자는 hr 하나다.

- 인수인계: `handovers`, `handover_items`, `handover_fields`, `handover_questions`
- 온보딩: `onboardings`, `onboarding_steps`
- 발령·퇴직: `assignment_changes`
- 명부·교대: `roster_entries`, `shifts`
- 공통: `audit_logs`, `idempotency`, `records`(결정이 없어 기록만 남긴 것)
- 목 전용(실제에서는 다른 서비스가 가진다): `mock_cards`(memory), `gate_decisions`(안전 레이어)

모든 테이블에 `tenant_id`를 둔다. 스키마는 전 문장 `IF NOT EXISTS`다.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

from ax_contracts.schemas import TENANT_ID_DEFAULT

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  context_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  result TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS records (
  record_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  context_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  source_refs TEXT NOT NULL,
  author TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mock_cards (
  card_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  data TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_decisions (
  idem_key TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  action TEXT NOT NULL,
  context_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  summary TEXT NOT NULL,
  params TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE TABLE IF NOT EXISTS areas (
  area TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}'
);
CREATE TABLE IF NOT EXISTS roster_entries (
  entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  area TEXT NOT NULL,
  role TEXT NOT NULL,
  member_id TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS shifts (
  shift_id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  area TEXT NOT NULL,
  member_id TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  handled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS handovers (
  handover_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  context_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  successor_id TEXT,
  area TEXT,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  completeness INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_items (
  handover_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  source_ref TEXT,
  PRIMARY KEY (handover_id, item_id)
);
CREATE TABLE IF NOT EXISTS handover_fields (
  handover_id TEXT NOT NULL,
  field TEXT NOT NULL,
  filled INTEGER NOT NULL,
  source TEXT NOT NULL,
  answer TEXT,
  PRIMARY KEY (handover_id, field)
);
CREATE TABLE IF NOT EXISTS handover_questions (
  question_id TEXT PRIMARY KEY,
  handover_id TEXT NOT NULL,
  context_id TEXT NOT NULL,
  assignee_id TEXT NOT NULL,
  question TEXT NOT NULL,
  gap_field TEXT NOT NULL,
  status TEXT NOT NULL,
  answer TEXT,
  asked_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  answered_at TEXT
);
CREATE TABLE IF NOT EXISTS onboardings (
  onboarding_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  context_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  area TEXT NOT NULL,
  handover_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onboarding_steps (
  onboarding_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  step_order INTEGER NOT NULL,
  title TEXT NOT NULL,
  card_id TEXT,
  done INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (onboarding_id, step_id)
);
CREATE TABLE IF NOT EXISTS assignment_changes (
  change_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT '{TENANT_ID_DEFAULT}',
  context_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  member_id TEXT NOT NULL,
  area TEXT NOT NULL,
  successor_id TEXT,
  status TEXT NOT NULL,
  handover_id TEXT,
  onboarding_id TEXT,
  checklist TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  previous TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
  member_id TEXT PRIMARY KEY,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS locks (
  key TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class Store:
    """SQLite 연결 하나를 스레드 잠금으로 감싼 얇은 래퍼."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> int:
        """쓰기 한 문장을 실행한다(자동 확정, `transaction()` 안에서는 묶음 단위로 확정).

        INSERT는 마지막 행 ID(없으면 영향 행 수), UPDATE·DELETE는 영향 행 수를 돌려준다.
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            if sql.lstrip().upper().startswith(("UPDATE", "DELETE")):
                return cur.rowcount
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def one(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @contextmanager
    def transaction(self):  # type: ignore[no-untyped-def]
        """여러 문장을 한 번에 확정한다. 중간에 예외가 나면 모두 되돌린다."""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
