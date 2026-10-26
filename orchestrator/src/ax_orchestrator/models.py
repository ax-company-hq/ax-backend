"""사건·단계 상태 모델과 plan 정의 (docs/notes/orchestrator/interface.md 3·5장)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


@dataclass
class Step:
    """plan의 한 단계. 엔진은 시나리오를 모르고 이 정의만 실행한다."""

    id: str
    skill: str
    label: str
    after: tuple[str, ...] = ()  # 선행 단계. 모두 성공해야 실행
    also_after: tuple[str, ...] = ()  # 선행 단계. 끝나기만 하면 실행
    build: Callable[[Incident], dict[str, Any]] = lambda inc: {}
    serial_group: str | None = None
    required: bool = True
    kind: str = "call"  # call | record(사건 종료 기록)
    keep: dict[str, str] = field(default_factory=dict)  # 결과 필드 → 사건에 보관할 이름
    # 호출하기 전에 "지금은 하지 않는다"를 판단한다: (상태, 사유 코드, 메모) 또는 None(그대로 호출)
    gate: Callable[[Incident], tuple[str, str, str] | None] = lambda inc: None
    # 성공한 결과를 보고 사람이 봐야 하면 이관 큐로 돌린다: (사유 코드, 메모) 또는 None
    verdict: Callable[[dict[str, Any]], tuple[str, str] | None] = lambda output: None
    timeout: float | None = None  # 이 단계만 마감을 길게 줄 때(예: 복구 확인 재시도) 초


@dataclass
class Plan:
    name: str
    steps: list[Step]


@dataclass
class StepRun:
    step_id: str
    skill: str
    label: str
    dept: str | None = None
    status: str = "pending"
    attempts: int = 0
    note: str = ""
    grade: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: dict[str, Any] = field(default_factory=dict)
    transferred_to: str | None = None
    reason_code: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "skill": self.skill,
            "label": self.label,
            "dept": self.dept,
            "status": self.status,
            "attempts": self.attempts,
            "note": self.note,
            "grade": self.grade,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "transferred_to": self.transferred_to,
            "reason_code": self.reason_code,
            "output": self.output,
        }


TERMINAL = {"completed", "failed", "skipped", "rejected_by_human", "transfer_queued"}


@dataclass
class Incident:
    context_id: str
    kind: str
    title: str
    component: str
    opened_at: datetime = field(default_factory=now)
    closed_at: datetime | None = None
    status: str = "open"
    steps: dict[str, StepRun] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    holds: dict[str, Any] = field(default_factory=dict)  # step_id -> Hold
    summary: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)  # 부서가 준 값 중 다른 부서에 넘길 것

    @staticmethod
    def new_context_id() -> str:
        return f"ctx_inc_{uuid.uuid4().hex[:12]}"

    def log(self, message: str) -> None:
        self.events.append({"at": iso(now()), "message": message})

    def view(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "kind": self.kind,
            "title": self.title,
            "component": self.component,
            "status": self.status,
            "opened_at": iso(self.opened_at),
            "closed_at": iso(self.closed_at),
            "steps": [s.view() for s in self.steps.values()],
            "events": self.events,
            "summary": self.summary,
            "metrics": self.metrics,
            "shared": self.shared,
            "pending_holds": [
                {"step_id": sid, "skill": self.steps[sid].skill, "label": self.steps[sid].label}
                for sid, hold in self.holds.items()
                if hold.pending
            ],
        }
