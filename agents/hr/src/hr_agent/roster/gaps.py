"""공백 점검(코드 규칙): 담당이 없는 영역, 교대 시각에 담당이 비는 영역.

- 담당 없음: 활성 담당(owner)이 한 명도 없는 영역.
- 교대 공백: 앞으로 `horizon_hours` 안에 끝나는 교대 시각에, 그 영역에서 다음 사람이 이어받지
  않는 경우(그 시각을 덮는 교대가 없음).
숫자와 판정은 모두 코드가 계산하고 LLM은 관여하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from hr_agent.roster import store as roster
from hr_agent.store import Store


@dataclass(frozen=True)
class Gaps:
    no_owner_areas: list[str] = field(default_factory=list)
    shift_gaps: list[tuple[str, datetime]] = field(default_factory=list)  # (영역, 비는 시각)

    @property
    def gap_areas(self) -> list[str]:
        return sorted(set(self.no_owner_areas) | {area for area, _ in self.shift_gaps})


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def compute_gaps(store: Store, now: datetime, horizon_hours: int = 24) -> Gaps:
    no_owner = [a for a in roster.list_areas(store) if not roster.owners(store, a)]
    horizon = now + timedelta(hours=horizon_hours)
    shift_gaps: list[tuple[str, datetime]] = []
    for area in roster.list_areas(store):
        if area in no_owner:
            continue
        rows = roster.shifts(store, area)
        spans = [(_parse(r["start_at"]), _parse(r["end_at"])) for r in rows]
        for _, end in sorted(spans, key=lambda s: s[1]):
            if not (now <= end <= horizon):
                continue
            covered = any(start <= end < other_end for start, other_end in spans)
            if not covered:
                shift_gaps.append((area, end))
    return Gaps(sorted(no_owner), sorted(shift_gaps, key=lambda g: (g[1], g[0])))
