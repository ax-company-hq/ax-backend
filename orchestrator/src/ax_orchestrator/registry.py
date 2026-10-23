"""Agent Card 수집·skill 레지스트리 (ORC-04).

시작 시 모든 부서 카드를 읽어 `skill → 부서` 표를 만든다. 수집에 실패한 부서는 `unavailable`로
표시하고, 그 부서의 skill은 "확인할 수 없음"으로 처리한다(사건은 멈추지 않는다).
같은 skill을 여러 부서가 내면 부서 이름 순으로 첫 부서를 쓰고 충돌을 기록한다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ax_contracts.schemas.agent_card import AgentCard

SELF = "orchestrator"
CardFetcher = Callable[[str], Awaitable[dict[str, Any] | None]]


@dataclass
class SkillEntry:
    dept: str
    skill: str
    grade: str
    verifier: str | None
    priority: str
    callable_by_orchestrator: bool


@dataclass
class Registry:
    skills: dict[str, SkillEntry] = field(default_factory=dict)
    cards: dict[str, AgentCard] = field(default_factory=dict)
    unavailable: dict[str, str] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)

    async def load(self, depts: list[str], fetch: CardFetcher) -> None:
        self.skills.clear()
        self.cards.clear()
        self.unavailable.clear()
        self.conflicts.clear()
        for dept in sorted(depts):
            try:
                raw = await fetch(dept)
            except Exception as exc:  # 연결 오류 등 — 사건을 멈추지 않는다
                self.unavailable[dept] = f"수집 실패: {type(exc).__name__}"
                continue
            if raw is None:
                self.unavailable[dept] = "수집 실패: 응답 없음"
                continue
            try:
                card = AgentCard.model_validate(raw)
            except ValueError:
                self.unavailable[dept] = "카드 형식 위반"
                continue
            if not card.skills:
                self.unavailable[dept] = "카드에 skill 없음"
                continue
            self.cards[dept] = card
            for skill in card.skills:
                if skill.id in self.skills:
                    self.conflicts.append(f"{skill.id}: {self.skills[skill.id].dept} / {dept}")
                    continue
                self.skills[skill.id] = SkillEntry(
                    dept=dept,
                    skill=skill.id,
                    grade=skill.grade,
                    verifier=skill.verifier,
                    priority=skill.priority,
                    callable_by_orchestrator=SELF in skill.allowed_requesters,
                )

    def find(self, skill: str) -> SkillEntry | None:
        return self.skills.get(skill)

    def alternatives(self, skill: str, exclude: str) -> list[SkillEntry]:
        """같은 skill 이름을 가진 다른 부서(이관용)."""
        return [
            SkillEntry(dept, s.id, s.grade, s.verifier, s.priority, SELF in s.allowed_requesters)
            for dept, card in sorted(self.cards.items())
            if dept != exclude
            for s in card.skills
            if s.id == skill
        ]

    def view(self) -> dict[str, Any]:
        return {
            "depts": [
                {
                    "name": name,
                    "owner": card.owner,
                    "skills": [
                        {
                            "id": s.id,
                            "grade": s.grade,
                            "priority": s.priority,
                            "orchestrator": SELF in s.allowed_requesters,
                        }
                        for s in card.skills
                    ],
                }
                for name, card in sorted(self.cards.items())
            ],
            "unavailable": self.unavailable,
            "conflicts": self.conflicts,
        }
