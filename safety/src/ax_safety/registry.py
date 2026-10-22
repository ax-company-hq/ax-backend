"""액션 등급 기준표 (SAF-02). 설정 파일 `grade_table.json`에서 읽는다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ax_contracts.schemas.execute import HoldReason

GRADE_TABLE_PATH = Path(__file__).with_name("grade_table.json")


@dataclass(frozen=True)
class ActionRule:
    grade: Literal["A", "B", "C"]
    requesters: frozenset[str]


@dataclass(frozen=True)
class GradeVerdict:
    """기준표 판정. `allowed`가 False면 거부하고, `hold_reason`에 이유를 담는다."""

    allowed: bool
    grade: Literal["A", "B", "C"]
    hold_reason: HoldReason | None = None
    threshold_version: int | None = None


class GradeTable:
    def __init__(self, version: int, rules: dict[str, ActionRule]) -> None:
        self.version = version
        self._rules = rules

    @classmethod
    def load(cls, path: Path = GRADE_TABLE_PATH) -> GradeTable:
        raw = json.loads(path.read_text(encoding="utf-8"))
        rules = {
            action: ActionRule(spec["grade"], frozenset(spec["requesters"]))
            for action, spec in raw["actions"].items()
        }
        return cls(raw["version"], rules)

    def judge(self, action: str, requester: str) -> GradeVerdict:
        rule = self._rules.get(action)
        if rule is None:
            # 미등록 액션은 C로 보류한다 (fail-closed)
            return GradeVerdict(False, "C", HoldReason.UNREGISTERED_ACTION, self.version)
        if requester not in rule.requesters:
            return GradeVerdict(False, rule.grade, None, self.version)
        return GradeVerdict(True, rule.grade, None, self.version)
