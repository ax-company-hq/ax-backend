"""LLM 호출 사용량 저장소(NFR-06)의 쓰기·읽기 구현.

레코드와 조회 형식은 계약(`ax_contracts.data_access.llm_usage`)이다. 쓰기는 LLM 래퍼의
`record_usage`만 하고, 읽는 쪽(dashboard-api, data)은 같은 형식의 읽기 조회만 쓴다.
공통 로그 `LogKind`는 응답 enum이라 값을 늘릴 수 없어서 따로 둔다.

저장소는 SQLite 파일 하나다(WAL). 위치는 환경변수 `AX_LLM_USAGE_DB`로 정하고, 비어 있으면 파일에
쓰지 않는다(표준 로거에만 남긴다). 프롬프트·응답·고객 원문은 담지 않는다(부서·모델 이름과 숫자만).
기록이 실패해도 LLM 호출은 멈추지 않는다(경고만 남긴다).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from typing import Protocol

from ax_contracts.data_access.llm_usage import LlmCallRecord, LlmUsageQuery, LlmUsageResult

DB_ENV = "AX_LLM_USAGE_DB"
_logger = logging.getLogger("ax.llm_usage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id        TEXT NOT NULL UNIQUE,
    tenant_id     TEXT NOT NULL,
    context_id    TEXT,
    occurred_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    department    TEXT NOT NULL,
    model         TEXT,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    success       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_context ON llm_calls (context_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_time ON llm_calls (occurred_at);
"""


class UsageStore(Protocol):
    def write(self, record: LlmCallRecord) -> None: ...

    def query(self, query: LlmUsageQuery) -> LlmUsageResult: ...


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SqliteUsageStore:
    """SQLite 파일(또는 `:memory:`) 저장소."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def write(self, record: LlmCallRecord) -> None:
        row = (
            record.log_id,
            record.tenant_id,
            record.context_id,
            _iso(record.occurred_at),
            record.source,
            record.department,
            record.model,
            record.input_tokens,
            record.output_tokens,
            int(record.success),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO llm_calls (log_id, tenant_id, context_id, occurred_at,"
                " source, department, model, input_tokens, output_tokens, success)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                row,
            )

    def query(self, query: LlmUsageQuery) -> LlmUsageResult:
        """조건에 맞는 최근 `limit`건을 오름차순으로 돌려준다. 더 있으면 `truncated`다."""
        where, args = ["tenant_id = ?"], [query.tenant_id]
        if query.context_ids:
            where.append(f"context_id IN ({','.join('?' * len(query.context_ids))})")
            args += query.context_ids
        if query.departments:
            where.append(f"department IN ({','.join('?' * len(query.departments))})")
            args += query.departments
        if query.from_ is not None:
            where.append("occurred_at >= ?")
            args.append(_iso(query.from_))
        if query.to is not None:
            where.append("occurred_at <= ?")
            args.append(_iso(query.to))
        sql = (
            "SELECT * FROM llm_calls WHERE " + " AND ".join(where) + " ORDER BY occurred_at DESC,"
            " seq DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [*args, query.limit + 1]).fetchall()
        truncated = len(rows) > query.limit
        records = [_record(r) for r in reversed(rows[: query.limit])]
        return LlmUsageResult(records=records, truncated=truncated)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _record(row: sqlite3.Row) -> LlmCallRecord:
    data = dict(row)
    data.pop("seq")
    data["success"] = bool(data["success"])
    return LlmCallRecord.model_validate(data)


_store: UsageStore | None = None
_store_key: str | None = None


def set_store(store: UsageStore | None) -> None:
    """저장소를 직접 지정한다(테스트·조립용). `None`이면 환경변수 설정으로 돌아간다."""
    global _store, _store_key
    _store = store
    _store_key = "explicit" if store is not None else None


def reset_store() -> None:
    """지정·캐시한 저장소를 버린다(테스트 초기화용)."""
    global _store, _store_key
    if isinstance(_store, SqliteUsageStore) and _store_key != "explicit":
        _store.close()
    _store, _store_key = None, None


def get_store() -> UsageStore | None:
    """현재 저장소. 직접 지정이 없으면 `AX_LLM_USAGE_DB` 파일을 연다(없으면 `None`)."""
    global _store, _store_key
    if _store_key == "explicit":
        return _store
    path = os.getenv(DB_ENV)
    if not path:
        return None
    if _store is None or _store_key != path:
        _store = SqliteUsageStore(path)
        _store_key = path
    return _store


def record_call(
    *,
    source: str,
    department: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    success: bool,
    context_id: str | None = None,
    tenant_id: str | None = None,
) -> None:
    """호출 1회의 사용량을 저장소에 남긴다. 실패해도 예외를 밖으로 내지 않는다."""
    try:
        fields = {
            "log_id": f"llm_{uuid.uuid4().hex[:16]}",
            "context_id": context_id,
            "occurred_at": datetime.now(UTC),
            "source": source,
            "department": department,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "success": success,
        }
        if tenant_id is not None:
            fields["tenant_id"] = tenant_id
        record = LlmCallRecord.model_validate(fields)
        _logger.info(record.model_dump_json())
        store = get_store()
        if store is not None:
            store.write(record)
    except Exception:  # 사용량 기록 때문에 LLM 호출이 멈추면 안 된다
        _logger.warning("LLM 사용량 기록 실패", exc_info=True)
