"""부서별 소통 탭: 공통 로그(`CommonLogQuery`/`CommonLogResult`)의 A2A 요청·응답을 한 줄로 만든다.

목록에는 코드가 만든 필드(누가·누구에게·skill·결과·시각·context_id·execution_id)만 담는다.
`payload`와 고객 원문은 한 줄에 넣지 않는다. 읽기는 `set_reader`로 꽂은 함수, 아니면 공통 로그
저장소(`AX_COMMON_LOG_DB`)를 쓰고, 둘 다 없으면(목 모드 제외) 503을 돌려준다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from ax_agent_template import common_log
from ax_contracts.data_access.common_log import CommonLogQuery, CommonLogResult, LogRecord
from ax_dashboard_api.envelope import ApiError, is_mock

CommunicationOutcome = Literal[
    "completed", "rejected", "failed", "held", "needs_human", "in_progress"
]

# 소통으로 보는 로그 종류. 이벤트 발행·기억 참조는 부서 간 대화가 아니다
COMM_KINDS = [
    "request_received",
    "result_sent",
    "task_status",
    "execute_decided",
    "escalated",  # 이관 큐로 넘긴 건은 `needs_human`으로 보인다
    "rejected",
]
MAX_LIMIT = 1000

# 팀 기준 부서 이름(.github/team.yml)과 에이전트 이름(Agent Card, 공통 로그의 `source`)이 다른 곳.
# DejaFix 어댑터는 팀에서는 `dejafix`(다른 이름 `infra-dejafix`)이고 에이전트 이름은 `infra`다.
# `dept`는 어느 쪽 이름으로 보내도 되고, 응답의 `from_agent`·`to_agent`는 에이전트 이름 그대로다.
DEPT_NAMES = {"dejafix": "infra", "infra-dejafix": "infra"}


def agent_name(dept: str) -> str:
    """`dept` 값을 공통 로그의 에이전트 이름으로 바꾼다."""
    return DEPT_NAMES.get(dept, dept)


LogReader = Callable[[CommonLogQuery], CommonLogResult]


class CommunicationLine(BaseModel):
    """소통 탭 한 줄. `execution_id`로 안전 레이어 결정과 잇는다."""

    log_id: str
    occurred_at: datetime
    from_agent: str
    to_agent: str | None = None
    skill: str | None = None
    outcome: CommunicationOutcome
    context_id: str | None = None
    execution_id: str | None = None


_reader: LogReader | None = None


def set_reader(reader: LogReader | None) -> None:
    """공통 로그 읽기 함수를 정한다 (None이면 해제)."""
    global _reader
    _reader = reader


def _mock_records() -> CommonLogResult:
    def rec(n: int, minute: int, **kw: object) -> LogRecord:
        return LogRecord.model_validate(
            {
                "log_id": f"log_mock_{n}",
                "context_id": "ctx_inc_mock1",
                "occurred_at": datetime(2026, 10, 22, 9, minute, tzinfo=UTC),
                **kw,
            }
        )

    return CommonLogResult(
        records=[
            rec(
                1,
                0,
                source="support",
                kind="request_received",
                skill="get_claim",
                task_status="working",
                requester="orchestrator",
            ),
            rec(
                2,
                1,
                source="support",
                kind="result_sent",
                skill="get_claim",
                task_status="completed",
                requester="orchestrator",
            ),
            rec(
                3,
                2,
                source="finance",
                kind="execute_decided",
                skill="hold_settlement",
                grade="C",
                task_status="input-required",
                requester="orchestrator",
                payload={"execution_id": "exe_mock1", "decision": "hold"},
            ),
            rec(
                4,
                3,
                source="marketing",
                kind="rejected",
                skill="issue_coupon",
                task_status="rejected",
                requester="support",
            ),
        ]
    )


def _read(query: CommonLogQuery) -> CommonLogResult:
    if _reader is not None:
        return _reader(query)
    if is_mock():
        return _mock_records()
    store = common_log.get_store()  # `AX_COMMON_LOG_DB` 파일 (compose의 공유 볼륨)
    if store is None:
        raise ApiError(
            503,
            "UPSTREAM_NOT_CONNECTED",
            "공통 로그 저장소가 연결되지 않았다 (AX_COMMON_LOG_DB, 목 모드는 DASHBOARD_MOCK=1)",
        )
    return store.query(query)


def _outcome(r: LogRecord) -> CommunicationOutcome:
    decision = r.payload.get("decision")
    if r.kind == "rejected" or r.task_status == "rejected":
        return "rejected"
    if r.task_status in ("failed", "canceled"):  # 거절(정책)과 달리 실행 오류·시간 초과
        return "failed"
    if r.kind == "execute_decided" and decision in ("hold", "held", "reject", "rejected"):
        return "held" if decision in ("hold", "held") else "rejected"
    if r.kind == "escalated" or r.task_status == "input-required":
        return "needs_human"
    if r.task_status == "completed":
        return "completed"
    return "in_progress"


def _line(r: LogRecord) -> CommunicationLine:
    execution_id = r.payload.get("execution_id")
    # 요청은 requester → source, 그 밖의 결과·결정은 source → requester 방향이다
    sent_by_requester = r.kind == "request_received"
    return CommunicationLine(
        log_id=r.log_id,
        occurred_at=r.occurred_at,
        from_agent=(r.requester or r.source) if sent_by_requester else r.source,
        to_agent=r.source if sent_by_requester else r.requester,
        skill=r.skill,
        outcome=_outcome(r),
        context_id=r.context_id,
        execution_id=execution_id if isinstance(execution_id, str) else None,
    )


def list_communications(
    dept: str | None, context_id: str | None, limit: int = MAX_LIMIT
) -> tuple[list[CommunicationLine], bool]:
    """(줄 목록 오름차순, 잘림 여부). `dept`가 보냈거나 받은 줄만 남긴다."""
    query = CommonLogQuery(
        context_ids=[context_id] if context_id else [],
        kinds=COMM_KINDS,  # type: ignore[arg-type]
        limit=min(max(limit, 1), MAX_LIMIT),
    )
    result = _read(query)
    lines = [_line(r) for r in result.records]
    if dept:
        wanted = agent_name(dept)
        lines = [x for x in lines if wanted in (x.from_agent, x.to_agent)]
    return lines, result.truncated
