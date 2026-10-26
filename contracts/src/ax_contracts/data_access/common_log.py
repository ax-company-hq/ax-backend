"""공통 로그 저장소(COM-08)의 레코드 형식과 읽기 조회 형식.

쓰기는 agent-template의 `log_event`만 한다. 읽기는 이 형식의 읽기 전용 조회만 쓴다
(dashboard-api DSH-01~04, data `kpi_summary`, memory 참조 로그 MEM-04).
다른 서비스 DB에는 직접 접근하지 않는다.
`payload`에는 사실만 담는다. 판단 문장과 외부 입력 원문은 넣지 않는다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime

LogKind = Literal[
    "request_received",
    "result_sent",
    "task_status",
    "execute_decided",
    "escalated",
    "rejected",
    "memory_referenced",
    "event_published",
    "llm_call",
]


class LogRecord(AxModel):
    """로그 한 줄. `context_id`는 사건 전 이벤트이면 None이다."""

    log_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str | None = None
    occurred_at: UtcDatetime
    source: str  # 에이전트 이름
    kind: LogKind
    skill: str | None = None
    # A2A Task 상태: working/completed/input-required/failed/canceled/rejected
    task_status: str | None = None
    grade: Literal["read", "A", "B", "C"] | None = None
    requester: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class CommonLogQuery(AxModel):
    """입력. `from`은 파이썬 예약어라 별칭으로 받는다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    context_ids: list[str] = Field(default_factory=list)
    from_: UtcDatetime | None = Field(default=None, alias="from")
    to: UtcDatetime | None = None
    kinds: list[LogKind] = Field(default_factory=list)
    limit: int = Field(default=1000, ge=1, le=5000)
    tenant_id: str = TENANT_ID_DEFAULT


class CommonLogResult(AxModel):
    """출력. `records`는 occurred_at 오름차순이다. limit에 걸려 잘렸으면 `truncated`가 True다."""

    records: list[LogRecord] = Field(default_factory=list)
    truncated: bool = False
