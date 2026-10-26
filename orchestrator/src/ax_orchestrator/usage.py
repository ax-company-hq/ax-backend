"""부서별 호출 사용량 기록.

총괄이 부서를 부를 때마다 (부서, skill, 시각, 응답 시간, 결과)를 기록한다. 화면의 부서별 사용량
그래프(호출 수·성공률·평균 응답 시간)는 전부 여기서 센 실측값이다. 부서 내부의 CPU·메모리는 총괄이
알 수 없으므로 그리지 않고, 총괄 쪽에서 본 호출만 보여 준다.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from ax_orchestrator.models import iso, now

WINDOW_SECONDS = 600  # 그래프 구간 (최근 10분)
BUCKET_SECONDS = 30  # 막대 하나의 길이
MAX_RECORDS = 5000
OK_STATUSES = {TaskStatus.COMPLETED.value}
WAITING_STATUSES = {TaskStatus.INPUT_REQUIRED.value}

Caller = Callable[[str, AgentRequest], Awaitable[AgentResult]]


@dataclass
class CallRecord:
    dept: str
    skill: str
    at: datetime
    duration_ms: int
    status: str  # TaskStatus 값 또는 "error"(호출 중 예외)
    outcome: str | None
    context_id: str
    idempotency_key: str | None

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES

    @property
    def waiting(self) -> bool:
        return self.status in WAITING_STATUSES


class UsageRecorder:
    def __init__(self, max_records: int = MAX_RECORDS) -> None:
        self._records: deque[CallRecord] = deque(maxlen=max_records)

    def add(self, record: CallRecord) -> None:
        self._records.append(record)

    def _window(self, dept: str, current: datetime, skill: str | None = None) -> list[CallRecord]:
        start = current - timedelta(seconds=WINDOW_SECONDS)
        return [
            r
            for r in self._records
            if r.dept == dept and r.at >= start and (skill is None or r.skill == skill)
        ]

    @staticmethod
    def _p95(values: list[int]) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    def summary(self, dept: str, current: datetime | None = None, skill: str | None = None) -> dict:
        current = current or now()
        rows = self._window(dept, current, skill)
        finished = [r for r in rows if not r.waiting]
        durations = [r.duration_ms for r in rows]
        keys = Counter(r.idempotency_key for r in rows if r.idempotency_key)
        return {
            "calls": len(rows),
            "errors": sum(1 for r in finished if not r.ok),
            "waiting": sum(1 for r in rows if r.waiting),
            # 사람·부서 입력을 기다리는 호출은 성공·실패 계산에서 뺀다
            "success_rate": (
                round(sum(1 for r in finished if r.ok) / len(finished), 3) if finished else None
            ),
            "avg_ms": round(sum(durations) / len(durations)) if durations else None,
            "p95_ms": self._p95(durations),
            "retries": sum(count - 1 for count in keys.values() if count > 1),
            "last_called_at": iso(max((r.at for r in rows), default=None)),
        }

    def series(
        self, dept: str, current: datetime | None = None, skill: str | None = None
    ) -> list[dict[str, Any]]:
        current = current or now()
        buckets = WINDOW_SECONDS // BUCKET_SECONDS
        start = current - timedelta(seconds=WINDOW_SECONDS)
        rows = self._window(dept, current, skill)
        out: list[dict[str, Any]] = []
        for i in range(buckets):
            lo = start + timedelta(seconds=i * BUCKET_SECONDS)
            hi = lo + timedelta(seconds=BUCKET_SECONDS)
            chunk = [r for r in rows if lo <= r.at < hi or (i == buckets - 1 and r.at >= hi)]
            out.append(
                {
                    "t": iso(lo),
                    "calls": len(chunk),
                    "errors": sum(1 for r in chunk if not r.ok and not r.waiting),
                    "avg_ms": round(sum(r.duration_ms for r in chunk) / len(chunk))
                    if chunk
                    else None,
                }
            )
        return out

    def recent(self, dept: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = [r for r in self._records if r.dept == dept][-limit:]
        return [
            {
                "at": iso(r.at),
                "skill": r.skill,
                "status": r.status,
                "outcome": r.outcome,
                "duration_ms": r.duration_ms,
                "context_id": r.context_id,
            }
            for r in reversed(rows)
        ]


class RecordingCaller:
    """`Engine(caller=...)`에 끼워서 모든 부서 호출을 기록한다. 결과는 그대로 돌려준다."""

    def __init__(self, inner: Caller, recorder: UsageRecorder) -> None:
        self.inner = inner
        self.recorder = recorder

    async def __call__(self, dept: str, req: AgentRequest) -> AgentResult:
        started = time.perf_counter()
        at = now()
        status, outcome = "error", None
        try:
            result = await self.inner(dept, req)
            status, outcome = result.status.value, result.outcome
            return result
        finally:
            self.recorder.add(
                CallRecord(
                    dept=dept,
                    skill=req.skill,
                    at=at,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    status=status,
                    outcome=outcome,
                    context_id=req.meta.context_id,
                    idempotency_key=req.meta.idempotency_key,
                )
            )
