"""발령·퇴직·입사의 코드 규칙: 담당 변경 계획, 담당 공백·중복 점검, 후임 후보, 확인 목록.

모든 판정은 코드가 한다. 변경은 먼저 "계획"(`Op` 목록)으로 만들어 복사본에 적용해 보고
(시뮬레이션), 담당(owner)이 없어지거나 둘 이상이 되는 영역이 없을 때만 실제로 적용한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from hr_agent.roster import store as roster
from hr_agent.store import Store

Op = tuple[str, str, str, bool]  # (영역, 역할, 담당자, 켬/끔)

CHECKLISTS: dict[str, list[str]] = {
    "shift": ["교대 시각과 후임을 확인한다", "인수인계를 확정한다"],
    "transfer": ["후임을 지정한다", "인수인계를 확정한다", "권한 변경 목록을 각 부서에 전달한다"],
    "offboarding": [
        "후임을 지정한다",
        "인수인계를 확정한다",
        "권한 회수 목록을 각 부서에 전달한다",
        "퇴직을 확정한다(사람 승인)",
    ],
    "join": [
        "온보딩 계획을 확인한다",
        "권한 부여 목록을 각 부서에 전달한다",
        "첫 담당 영역을 확인한다",
    ],
}


def access_list(kind: str, area: str) -> list[str]:
    verb = "grant" if kind == "join" else "revoke"
    return [f"{verb}:{area}/{res}" for res in ("repo", "deploy", "oncall")]


def plan_ops(
    kind: str, member: str, area: str, successor: str, area_entries: list[tuple[str, str]]
) -> list[Op]:
    """`area_entries`는 그 영역의 지금 (역할, 담당자) 목록."""
    if kind == "join":
        has_owner = any(role == "owner" for role, _ in area_entries)
        return [(area, "backup" if has_owner else "owner", member, True)]
    held = {role for role, who in area_entries if who == member}
    taken = {(role, who) for role, who in area_entries}
    ops: list[Op] = []
    for role in sorted(held):
        if (role, successor) not in taken:
            ops.append((area, role, successor, True))
            if role == "owner" and ("backup", successor) in taken:  # 예비 담당이 담당으로 승격
                ops.append((area, "backup", successor, False))
        if kind == "shift":
            if role == "owner":  # 교대: 맡은 담당은 넘기되 예비 담당으로 남는다
                ops.append((area, "owner", member, False))
                ops.append((area, "backup", member, True))
        else:
            ops.append((area, role, member, False))
    return ops


def apply_to_map(entries: dict[str, set[tuple[str, str]]], ops: list[Op]) -> None:
    for area, role, who, on in ops:
        bucket = entries.setdefault(area, set())
        if on:
            bucket.add((role, who))
        else:
            bucket.discard((role, who))


def entries_map(store: Store) -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {a: set() for a in roster.list_areas(store)}
    for e in roster.entries(store):
        out.setdefault(e.area, set()).add((e.role, e.member_id))
    return out


@dataclass(frozen=True)
class Coverage:
    gap_areas: list[str]
    duplicate_areas: list[str]

    @property
    def ok(self) -> bool:
        return not self.gap_areas and not self.duplicate_areas


def check_coverage(entries: dict[str, set[tuple[str, str]]], areas: list[str]) -> Coverage:
    gaps, dups = [], []
    for area in sorted(areas):
        owners = [who for role, who in entries.get(area, set()) if role == "owner"]
        if not owners:
            gaps.append(area)
        elif len(set(owners)) > 1:
            dups.append(area)
    return Coverage(gaps, dups)


def simulate(
    store: Store, ops: list[Op], extra_removals: list[str] | None = None
) -> tuple[Coverage, dict[str, set[tuple[str, str]]]]:
    """ops를 복사본에 적용한 뒤, 건드린 영역(과 제거 대상의 모든 영역)의 공백·중복을 본다."""
    sim = entries_map(store)
    apply_to_map(sim, ops)
    touched = {area for area, *_ in ops}
    for who in extra_removals or []:
        for area, bucket in sim.items():
            if any(w == who for _r, w in bucket):
                touched.add(area)
                sim[area] = {(r, w) for r, w in bucket if w != who}
    return check_coverage(sim, sorted(touched)), sim


def successor_candidates(store: Store, member: str, area: str) -> list[str]:
    """같은 영역 예비 담당 → 담당 영역이 없는 사람 → 담당 영역이 적은 사람 순, 최대 3명."""
    in_area = {e.member_id: e.role for e in roster.entries(store, area)}
    ranked = []
    for who in roster.active_members(store):
        if who == member or in_area.get(who) == "owner":
            continue
        n_areas = len(roster.areas_of(store, who))
        group = 0 if who in in_area else (1 if n_areas == 0 else 2)
        ranked.append((group, n_areas, who))
    return [who for _g, _n, who in sorted(ranked)][:3]
