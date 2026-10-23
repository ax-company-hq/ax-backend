"""한 번 실행(run)의 원시 결과. 상태 판정(pass/pending/broken/regressed)은 judge.py가 한다."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Raw = Literal["pass", "fail", "absent", "blocked", "pending"]


@dataclass
class Outcome:
    """state: pass 통과 / fail 실패 / absent 구현 없음 / blocked 앞 단계 때문에 못 감 /
    pending 검사 불가."""

    state: Raw
    note: str = ""
    blocked_by: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> Outcome:
        return cls(**data)
