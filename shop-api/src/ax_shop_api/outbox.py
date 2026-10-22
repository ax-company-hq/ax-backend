"""이벤트 outbox. 이벤트는 주문·결제와 같은 트랜잭션에 저장하고, 저장 뒤 `events.publish`로 보낸다.

발행에 실패하면(Redis 연동 전 포함) 미발행으로 남겨 두고 다음 `flush`에서 다시 보낸다
(최소 한 번 전달). 받는 쪽은 같은 `event_id`를 두 번 처리하지 않는다.
모든 이벤트는 `events.publish`로만 발행하고 별도 HTTP 엔드포인트는 만들지 않는다.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ax_agent_template.events import publish
from ax_contracts.schemas import EventEnvelope, EventType
from ax_shop_api.db import TENANT_ID, Db

SOURCE = "shop-api"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def enqueue(
    conn: sqlite3.Connection, event_type: EventType, data: dict[str, Any], key: str
) -> None:
    """`key`는 `<type>:<대상 ID>`다. 같은 키는 한 번만 저장한다(이벤트 ID도 키에서 만든다)."""
    envelope = EventEnvelope(
        event_id=f"evt_{hashlib.sha256(key.encode()).hexdigest()[:12]}",
        type=event_type,
        occurred_at=datetime.now(UTC),
        tenant_id=TENANT_ID,
        context_id=None,  # 사건 번호는 통합 에이전트가 발급한다
        source=SOURCE,
        idempotency_key=key,
        data=data,
    )
    conn.execute(
        "INSERT OR IGNORE INTO outbox_events(event_id, payload, tenant_id, context_id, created_at) "
        "VALUES (?, ?, ?, NULL, ?)",
        (envelope.event_id, envelope.model_dump_json(), TENANT_ID, _now()),
    )


async def flush(db: Db) -> int:
    """미발행 이벤트를 발행 순서대로 보낸다. 실패하면 멈추고 보낸 개수를 돌려준다."""
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT event_id, payload FROM outbox_events WHERE sent_at IS NULL ORDER BY rowid"
        ).fetchall()
    sent = 0
    for row in rows:
        try:
            await publish(EventEnvelope.model_validate_json(row["payload"]))
        except NotImplementedError:  # Redis 연동 전(TODO). 미발행으로 남겨 둔다
            break
        with db.tx() as conn:
            conn.execute(
                "UPDATE outbox_events SET sent_at = ? WHERE event_id = ?", (_now(), row["event_id"])
            )
        sent += 1
    return sent
