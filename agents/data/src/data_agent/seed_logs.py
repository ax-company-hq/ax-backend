"""SCN-L용 공통 로그 시드 (`DATA_MOCK=1`). 같은 유형(결제 장애)의 사건 2건. 결정적이다.

- 1차 `ctx_inc_seed_001`: 회사 기억 참조 없음, 탐지 → incident.resolved 9분, 이관 1건.
- 2차 `ctx_inc_seed_002`: `memory_referenced` 있음, 탐지 → incident.resolved 6분, 이관 없음.
- 사건이 아닌 기록(context_id 없음, `ctx_req_`)은 KPI에서 빠지는지 확인하는 용도다.

payload 모양은 실제 기록 코드와 같다: 서버 `result_sent`(skill·status·outcome),
`events.publish`의 `event_published`(event_type·event_id), memory `memory_referenced`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ax_contracts.data_access.common_log import LogKind, LogRecord

CTX_FIRST = "ctx_inc_seed_001"
CTX_SECOND = "ctx_inc_seed_002"
CTX_REQUEST = "ctx_req_seed_001"

_counter = 0


def _t(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def _rec(
    context_id: str | None,
    at: datetime,
    source: str,
    kind: LogKind,
    *,
    skill: str | None = None,
    task_status: str | None = None,
    grade: str | None = None,
    payload: dict[str, Any] | None = None,
) -> LogRecord:
    global _counter
    _counter += 1
    return LogRecord(
        log_id=f"log_seed_{_counter:03d}",
        context_id=context_id,
        occurred_at=at,
        source=source,
        kind=kind,
        skill=skill,
        task_status=task_status,
        grade=grade,  # type: ignore[arg-type]
        payload=payload or {},
    )


def _done(ctx: str, at: datetime, source: str, skill: str) -> LogRecord:
    """받는 쪽 서버가 남기는 완료 기록."""
    return _rec(
        ctx,
        at,
        source,
        "result_sent",
        skill=skill,
        task_status="completed",
        payload={"skill": skill, "status": "completed", "outcome": None},
    )


def _event(ctx: str, at: datetime, event_type: str, n: int) -> LogRecord:
    return _rec(
        ctx,
        at,
        "orchestrator",
        "event_published",
        payload={"event_type": event_type, "event_id": f"evt_seed_{n:03d}"},
    )


def _rejected(ctx: str | None, at: datetime, reason: str) -> LogRecord:
    return _rec(ctx, at, "safety", "rejected", payload={"reason_code": reason})


SEED_LOGS: list[LogRecord] = [
    # ----- 1차 (21일 05:00 탐지, 05:09 incident.resolved) -----
    _rec(CTX_FIRST, _t(21, 5, 0), "orchestrator", "request_received", skill="impact_analysis"),
    _event(CTX_FIRST, _t(21, 5, 0, 30), "incident.opened", 1),
    _done(CTX_FIRST, _t(21, 5, 1), "marketing", "publish_notice"),  # 점검 공지
    _rec(CTX_FIRST, _t(21, 5, 2), "infra", "execute_decided", grade="A"),
    _rec(CTX_FIRST, _t(21, 5, 3), "support", "execute_decided", grade="B"),
    _rec(CTX_FIRST, _t(21, 5, 4), "marketing", "execute_decided", grade="C"),
    _rejected(CTX_FIRST, _t(21, 5, 4, 30), "UNAUTHORIZED"),
    _rejected(CTX_FIRST, _t(21, 5, 5), "FORBIDDEN_SKILL"),
    _rec(CTX_FIRST, _t(21, 5, 6), "orchestrator", "escalated", payload={"to": "human"}),
    _done(CTX_FIRST, _t(21, 5, 8), "infra", "verify_recovery"),
    _event(CTX_FIRST, _t(21, 5, 9), "incident.resolved", 2),
    _done(CTX_FIRST, _t(21, 5, 11), "marketing", "publish_notice"),  # 정상화 공지
    # ----- 사건이 아닌 기록 -----
    _rejected(None, _t(21, 12, 0), "UNAUTHORIZED"),
    _rec(CTX_REQUEST, _t(21, 13, 0), "hr", "execute_decided", grade="A"),
    # ----- 2차 (22일 03:00 탐지, 03:06 incident.resolved, 회사 기억 참조) -----
    _rec(CTX_SECOND, _t(22, 3, 0), "orchestrator", "request_received", skill="impact_analysis"),
    _event(CTX_SECOND, _t(22, 3, 0, 20), "incident.opened", 3),
    _rec(
        CTX_SECOND,
        _t(22, 3, 0, 40),
        "memory",
        "memory_referenced",
        payload={"requester": "orchestrator", "card_ids": ["card_seed_001"], "hit_count": 1},
    ),
    _done(CTX_SECOND, _t(22, 3, 1), "marketing", "publish_notice"),
    _rec(CTX_SECOND, _t(22, 3, 2), "infra", "execute_decided", grade="A"),
    _rec(CTX_SECOND, _t(22, 3, 3), "support", "execute_decided", grade="A"),
    _rec(CTX_SECOND, _t(22, 3, 4), "marketing", "execute_decided", grade="B"),
    _rejected(CTX_SECOND, _t(22, 3, 4, 30), "INJECTION_SUSPECTED"),
    _done(CTX_SECOND, _t(22, 3, 5), "infra", "verify_recovery"),
    _event(CTX_SECOND, _t(22, 3, 6), "incident.resolved", 4),
    _done(CTX_SECOND, _t(22, 3, 7), "marketing", "publish_notice"),
]

# 정답 상수 (SCN-L). 숫자는 위 시드에서 손으로 셌다.
EXPECTED_MTTR_FIRST = 540  # 9분
EXPECTED_MTTR_SECOND = 360  # 6분
EXPECTED_FASTER_MINUTES = 3
EXPECTED_USED_MEMORY = (False, True)
EXPECTED_RESOLVED_NOTICE_AT = (_t(21, 5, 11), _t(22, 3, 7))
EXPECTED_AUTO_HANDLED_RATE = 0.5
EXPECTED_GRADE_COUNTS = {"A": 3, "B": 2, "C": 1}
EXPECTED_BLOCKED_COUNTS = {"UNAUTHORIZED": 1, "FORBIDDEN_SKILL": 1, "INJECTION_SUSPECTED": 1}
EXPECTED_ESCALATION_COUNT = 1
