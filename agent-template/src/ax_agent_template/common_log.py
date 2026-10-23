"""공통 로그 저장소(COM-08)의 쓰기·읽기 구현.

레코드와 조회 형식은 계약(`ax_contracts.data_access.common_log`)이다. 쓰기는 `log_event`만 하고,
읽는 쪽(dashboard-api 등)은 `CommonLogStore.query`처럼 같은 형식의 읽기 조회만 쓴다.

저장소는 SQLite 파일 하나다(WAL). 위치는 환경변수 `AX_COMMON_LOG_DB`로 정하고, 비어 있으면 파일에
쓰지 않는다(표준 로거에만 남긴다). 여러 에이전트 프로세스가 같은 파일(공유 볼륨)에 쓸 수 있다.
Postgres로 옮길지는 팀 결정 사항이라 `LogStore` 프로토콜만 지키면 바꿔 끼울 수 있다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any, Protocol, get_args

from ax_contracts.data_access.common_log import (
    CommonLogQuery,
    CommonLogResult,
    LogKind,
    LogRecord,
)

DB_ENV = "AX_COMMON_LOG_DB"
LOG_KINDS: frozenset[str] = frozenset(get_args(LogKind))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS common_logs (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id      TEXT NOT NULL UNIQUE,
    tenant_id   TEXT NOT NULL,
    context_id  TEXT,
    occurred_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    skill       TEXT,
    task_status TEXT,
    grade       TEXT,
    requester   TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_common_logs_context ON common_logs (context_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_common_logs_time ON common_logs (occurred_at);
"""


class LogStore(Protocol):
    def write(self, record: LogRecord) -> None: ...

    def query(self, query: CommonLogQuery) -> CommonLogResult: ...


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SqliteLogStore:
    """SQLite 파일(또는 `:memory:`) 저장소."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def write(self, record: LogRecord) -> None:
        row = (
            record.log_id,
            record.tenant_id,
            record.context_id,
            _iso(record.occurred_at),
            record.source,
            record.kind,
            record.skill,
            record.task_status,
            record.grade,
            record.requester,
            json.dumps(record.payload, ensure_ascii=False, default=str),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO common_logs (log_id, tenant_id, context_id, occurred_at,"
                " source, kind, skill, task_status, grade, requester, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )

    def query(self, query: CommonLogQuery) -> CommonLogResult:
        """조건에 맞는 최근 `limit`건을 오름차순으로 돌려준다. 더 있으면 `truncated`다."""
        where, args = ["tenant_id = ?"], [query.tenant_id]
        if query.context_ids:
            where.append(f"context_id IN ({','.join('?' * len(query.context_ids))})")
            args += query.context_ids
        if query.from_ is not None:
            where.append("occurred_at >= ?")
            args.append(_iso(query.from_))
        if query.to is not None:
            where.append("occurred_at <= ?")
            args.append(_iso(query.to))
        if query.kinds:
            where.append(f"kind IN ({','.join('?' * len(query.kinds))})")
            args += list(query.kinds)
        sql = (
            "SELECT * FROM common_logs WHERE " + " AND ".join(where) + " ORDER BY occurred_at DESC,"
            " seq DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*args, query.limit + 1]).fetchall()
        truncated = len(rows) > query.limit
        records = [_record(r) for r in reversed(rows[: query.limit])]
        return CommonLogResult(records=records, truncated=truncated)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _record(row: sqlite3.Row) -> LogRecord:
    data: dict[str, Any] = dict(row)
    data.pop("seq")
    data["payload"] = json.loads(data["payload"])
    return LogRecord.model_validate(data)


_store: LogStore | None = None
_store_key: str | None = None


def set_store(store: LogStore | None) -> None:
    """저장소를 직접 지정한다(테스트·조립용). `None`이면 환경변수 설정으로 돌아간다."""
    global _store, _store_key
    _store = store
    _store_key = "explicit" if store is not None else None


def reset_store() -> None:
    """지정·캐시한 저장소를 버린다(테스트 초기화용)."""
    global _store, _store_key
    if isinstance(_store, SqliteLogStore) and _store_key != "explicit":
        _store.close()
    _store, _store_key = None, None


def get_store() -> LogStore | None:
    """현재 저장소. 직접 지정이 없으면 `AX_COMMON_LOG_DB` 파일을 연다(없으면 `None`)."""
    global _store, _store_key
    if _store_key == "explicit":
        return _store
    path = os.getenv(DB_ENV)
    if not path:
        return None
    if _store is None or _store_key != path:
        _store = SqliteLogStore(path)
        _store_key = path
    return _store
