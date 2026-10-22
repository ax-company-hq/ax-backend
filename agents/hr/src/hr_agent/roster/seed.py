"""가상 명부 시드(공개 레포라 가상 데이터만 쓴다): 영역·담당·교대 일정.

담당자 ID(`emp_01`…)는 planning 목 데이터(`clients/planning.py`)와 같은 값이다. 교대 시각은
시계 기준 날짜의 전날·당일·다음 날에 대해 만들어서, 어느 시각에 보아도 일정이 이어진다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from hr_agent.config import Clock
from hr_agent.store import Store

AREAS = ["payment", "search", "catalog", "support-desk", "infra-ops", "inventory"]

# (영역, 역할, 담당자)
ENTRIES: list[tuple[str, str, str]] = [
    ("payment", "owner", "emp_01"),
    ("payment", "backup", "emp_02"),
    ("search", "owner", "emp_03"),
    ("search", "backup", "emp_04"),
    ("catalog", "owner", "emp_05"),  # 예비 담당이 없다
    ("support-desk", "owner", "emp_06"),
    ("support-desk", "backup", "emp_07"),
    ("infra-ops", "owner", "emp_08"),
    ("infra-ops", "backup", "emp_09"),
    # inventory: 담당 없음 (공백 시연용). emp_10은 아직 어느 영역에도 없다(입사 시연용).
]

# (영역, 담당자, 시작 시, 끝 시): 끝 시가 시작 시보다 작거나 같으면 다음 날로 넘어간다 (UTC).
SHIFT_PATTERN: list[tuple[str, str, int, int]] = [
    ("payment", "emp_01", 9, 18),
    ("payment", "emp_02", 18, 9),
    ("search", "emp_03", 9, 18),
    ("search", "emp_04", 18, 9),
    ("catalog", "emp_05", 9, 18),  # 18시 이후에는 아무도 없다 (교대 공백 시연용)
    ("support-desk", "emp_06", 9, 18),
    ("support-desk", "emp_07", 18, 9),
    ("infra-ops", "emp_08", 9, 21),
    ("infra-ops", "emp_09", 21, 9),
]


def seed_roster(store: Store, clock: Clock) -> bool:
    """명부가 비어 있을 때만 시드를 넣는다. 넣었으면 True."""
    if store.one("SELECT area FROM areas LIMIT 1"):
        return False
    with store.transaction():
        for area in AREAS:
            store.execute("INSERT INTO areas(area) VALUES (?)", (area,))
        for area, role, member in ENTRIES:
            store.execute(
                "INSERT INTO roster_entries(area, role, member_id) VALUES (?, ?, ?)",
                (area, role, member),
            )
        today = clock().date()
        for offset in (-1, 0, 1):
            day = today + timedelta(days=offset)
            for area, member, start_h, end_h in SHIFT_PATTERN:
                start = datetime.combine(day, time(start_h), tzinfo=clock().tzinfo)
                end_day = day + timedelta(days=1 if end_h <= start_h else 0)
                end = datetime.combine(end_day, time(end_h), tzinfo=clock().tzinfo)
                store.execute(
                    "INSERT INTO shifts(area, member_id, start_at, end_at) VALUES (?, ?, ?, ?)",
                    (area, member, start.isoformat(), end.isoformat()),
                )
    return True
