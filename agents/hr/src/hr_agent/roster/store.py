"""명부 읽기·쓰기: 영역별 담당(`roster_entries`)과 교대 일정(`shifts`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hr_agent.store import Store


@dataclass(frozen=True)
class Entry:
    area: str
    role: str
    member_id: str


def list_areas(store: Store) -> list[str]:
    return [r["area"] for r in store.all("SELECT area FROM areas ORDER BY area")]


def entries(store: Store, area: str | None = None, member_id: str | None = None) -> list[Entry]:
    """활성 담당 목록. 담당(owner)이 먼저, 그다음 ID 순이라 결과가 늘 같다."""
    sql = "SELECT area, role, member_id FROM roster_entries WHERE active = 1"
    params: list[Any] = []
    if area is not None:
        sql += " AND area = ?"
        params.append(area)
    if member_id is not None:
        sql += " AND member_id = ?"
        params.append(member_id)
    sql += " ORDER BY area, CASE role WHEN 'owner' THEN 0 ELSE 1 END, member_id"
    return [Entry(r["area"], r["role"], r["member_id"]) for r in store.all(sql, params)]


def owners(store: Store, area: str) -> list[str]:
    return [e.member_id for e in entries(store, area) if e.role == "owner"]


def areas_of(store: Store, member_id: str) -> list[str]:
    return sorted({e.area for e in entries(store, member_id=member_id)})


def shifts(store: Store, area: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT shift_id, area, member_id, start_at, end_at, handled FROM shifts"
    params: list[Any] = []
    if area is not None:
        sql += " WHERE area = ?"
        params.append(area)
    return store.all(sql + " ORDER BY start_at, area, member_id", params)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def current_or_next_shift(
    store: Store, area: str, member_id: str, now: datetime
) -> tuple[datetime, datetime] | None:
    """담당자의 지금 진행 중인 교대(없으면 다음 교대)의 (시작, 끝)."""
    rows = [
        (_parse(r["start_at"]), _parse(r["end_at"]))
        for r in shifts(store, area)
        if r["member_id"] == member_id and _parse(r["end_at"]) > now
    ]
    return min(rows) if rows else None


def set_active(store: Store, area: str, role: str, member_id: str, active: bool) -> None:
    """담당 한 줄을 켜거나 끈다. 없으면 켤 때만 새로 만든다."""
    found = store.one(
        "SELECT entry_id FROM roster_entries WHERE area = ? AND role = ? AND member_id = ?",
        (area, role, member_id),
    )
    if found:
        store.execute(
            "UPDATE roster_entries SET active = ? WHERE entry_id = ?",
            (1 if active else 0, found["entry_id"]),
        )
    elif active:
        store.execute(
            "INSERT INTO roster_entries(area, role, member_id) VALUES (?, ?, ?)",
            (area, role, member_id),
        )


def known_member(store: Store, member_id: str) -> bool:
    return store.one("SELECT 1 FROM members WHERE member_id = ?", (member_id,)) is not None


def active_members(store: Store) -> list[str]:
    rows = store.all("SELECT member_id FROM members WHERE active = 1 ORDER BY member_id")
    return [r["member_id"] for r in rows]


def add_member(store: Store, member_id: str) -> None:
    store.execute("INSERT OR IGNORE INTO members(member_id, active) VALUES (?, 1)", (member_id,))
    store.execute("UPDATE members SET active = 1 WHERE member_id = ?", (member_id,))


def deactivate_member(store: Store, member_id: str) -> None:
    store.execute("UPDATE members SET active = 0 WHERE member_id = ?", (member_id,))
