"""감사 로그: hr이 한 일을 `context_id` 기준으로 남긴다(근거 점검·사후 확인의 재료).

DB(`audit_logs`)와 템플릿의 `log_event`(공통 로그)에 같이 남긴다.
"""

from __future__ import annotations

from typing import Any

from ax_agent_template.logging import log_event
from ax_contracts.schemas import TENANT_ID_DEFAULT
from hr_agent.config import Clock
from hr_agent.store import Store
from hr_agent.util import dumps, loads


class AuditLog:
    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def record(
        self,
        context_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        tenant_id: str = TENANT_ID_DEFAULT,
    ) -> None:
        data = payload or {}
        self._store.execute(
            "INSERT INTO audit_logs(tenant_id, context_id, kind, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (tenant_id, context_id, kind, dumps(data), self._clock().isoformat()),
        )
        log_event(context_id, tenant_id, kind, data)

    def list(self, context_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if context_id:
            rows = self._store.all(
                "SELECT * FROM audit_logs WHERE context_id = ? ORDER BY id LIMIT ?",
                (context_id, limit),
            )
        else:
            rows = self._store.all("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = loads(row["payload"], {})
        return rows

    def kinds(self, context_id: str) -> list[str]:
        return [row["kind"] for row in self.list(context_id)]

    def contexts(self, limit: int = 50) -> list[dict[str, Any]]:
        """감사 기록이 있는 사건(`context_id`) 목록: 최근 기록이 있는 순서, 건수와 마지막 시각."""
        return self._store.all(
            "SELECT context_id, COUNT(*) AS entries, MIN(created_at) AS first_at,"
            " MAX(created_at) AS last_at FROM audit_logs GROUP BY context_id"
            " ORDER BY MAX(id) DESC LIMIT ?",
            (limit,),
        )

    def timeline(self, context_id: str) -> list[dict[str, Any]]:
        """한 사건에서 hr이 한 일을 시간순(기록 순)으로 돌려준다. 값은 기록된 그대로다."""
        rows = self.list(context_id, limit=1000)
        return [
            {"seq": n, "at": r["created_at"], "kind": r["kind"], "payload": r["payload"]}
            for n, r in enumerate(rows, start=1)
        ]
