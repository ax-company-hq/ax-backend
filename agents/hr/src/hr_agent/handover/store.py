"""인수인계 저장·조회(hr 소유 테이블: handovers, handover_items/fields/questions)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ax_contracts.schemas import HandoverItem, HandoverQuestion
from hr_agent.handover.fields import FIELDS, completeness
from hr_agent.store import Store


def get(store: Store, handover_id: str) -> dict[str, Any] | None:
    return store.one("SELECT * FROM handovers WHERE handover_id = ?", (handover_id,))


def items(store: Store, handover_id: str) -> list[HandoverItem]:
    rows = store.all(
        "SELECT item_id, kind, title, source_ref FROM handover_items WHERE handover_id = ?"
        " ORDER BY rowid",
        (handover_id,),
    )
    return [HandoverItem(**r) for r in rows]


def fields(store: Store, handover_id: str) -> dict[str, dict[str, Any]]:
    rows = store.all("SELECT * FROM handover_fields WHERE handover_id = ?", (handover_id,))
    return {r["field"]: r for r in rows}


def filled_count(store: Store, handover_id: str) -> int:
    return sum(1 for r in fields(store, handover_id).values() if r["filled"])


def current_completeness(store: Store, handover_id: str) -> int:
    return completeness(filled_count(store, handover_id), len(FIELDS))


def open_questions(store: Store, handover_id: str) -> list[HandoverQuestion]:
    rows = store.all(
        "SELECT question_id, context_id, assignee_id, question, gap_field FROM handover_questions"
        " WHERE handover_id = ? AND status = 'open' ORDER BY rowid",
        (handover_id,),
    )
    return [HandoverQuestion(**r) for r in rows]


def question(store: Store, question_id: str) -> dict[str, Any] | None:
    return store.one("SELECT * FROM handover_questions WHERE question_id = ?", (question_id,))


def reask_stale(store: Store, handover_id: str, now: datetime, ttl_minutes: int) -> list[str]:
    """답이 없는 질문이 `ttl_minutes`를 넘기면 다시 묻는다(시각을 새로 잡고 횟수를 올린다)."""
    again: list[str] = []
    for q in store.all(
        "SELECT question_id, question, asked_count, created_at FROM handover_questions"
        " WHERE handover_id = ? AND status = 'open'"
        " AND handover_id IN (SELECT handover_id FROM handovers WHERE status != 'confirmed')",
        (handover_id,),
    ):
        if now - datetime.fromisoformat(q["created_at"]) < timedelta(minutes=ttl_minutes):
            continue
        base = (
            q["question"].split(") ", 1)[1]
            if q["question"].startswith("(다시 묻습니다")
            else q["question"]
        )
        count = q["asked_count"] + 1
        store.execute(
            "UPDATE handover_questions SET asked_count = ?, created_at = ?, question = ?"
            " WHERE question_id = ?",
            (count, now.isoformat(), f"(다시 묻습니다 {count}번째) {base}", q["question_id"]),
        )
        again.append(q["question_id"])
    return again


def set_status(store: Store, handover_id: str, status: str, now: str) -> None:
    store.execute(
        "UPDATE handovers SET status = ?, updated_at = ? WHERE handover_id = ?",
        (status, now, handover_id),
    )


def refresh(store: Store, handover_id: str, now: str) -> tuple[int, str]:
    """완성도와 상태(`ready`/`input_required`)를 다시 계산해 저장한다. 확정·보류 상태는 유지."""
    pct = current_completeness(store, handover_id)
    row = get(store, handover_id)
    assert row is not None
    status = row["status"]
    if status not in ("confirmed", "held"):
        status = "ready" if pct == 100 else "input_required"
    store.execute(
        "UPDATE handovers SET completeness = ?, status = ?, updated_at = ? WHERE handover_id = ?",
        (pct, status, now, handover_id),
    )
    return pct, status


def all_handovers(store: Store) -> list[dict[str, Any]]:
    return store.all("SELECT * FROM handovers ORDER BY created_at, handover_id")
