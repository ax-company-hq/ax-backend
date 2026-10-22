"""skill 핸들러가 `AgentResult`를 만들 때 쓰는 공통 도구."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    Evidence,
    ExecuteDecision,
    InputReason,
    RequestMeta,
    TaskStatus,
)
from hr_agent.config import Clock


def dump(output: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return output.model_dump(mode="json") if isinstance(output, BaseModel) else output


def make_evidence(
    clock: Clock, summary: str, refs: list[str] | None = None, queries: list[str] | None = None
) -> Evidence:
    """근거는 조회 시각과 참조 ID를 함께 담는다. 근거가 없으면 요약에 그 사실을 적는다."""
    return Evidence(
        queries=queries or [],
        data_summary=summary,
        collected_at=clock(),
        ref_ids=refs or [],
    )


def completed(
    request: AgentRequest,
    output: BaseModel | dict[str, Any],
    *,
    outcome: str | None = None,
    evidence: Evidence | None = None,
    decision_reason: str | None = None,
    alternatives: list[str] | None = None,
    fallback: bool = False,
) -> AgentResult:
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=outcome,
        output=dump(output),
        evidence=evidence or Evidence(),
        decision_reason=decision_reason,
        alternatives_considered=alternatives or [],
        fallback=fallback,
    )


def input_required(
    request: AgentRequest,
    output: BaseModel | dict[str, Any],
    *,
    reason: InputReason = InputReason.HUMAN_INPUT,
    evidence: Evidence | None = None,
    fallback: bool = False,
) -> AgentResult:
    """사람(인계자) 입력을 기다린다. 질문은 `output`에 담고, 대기 상태는 hr이 저장한다."""
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.INPUT_REQUIRED,
        reason_code=reason,
        output=dump(output),
        evidence=evidence or Evidence(),
        fallback=fallback,
    )


def failed(request: AgentRequest, outcome: str, message: str) -> AgentResult:
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.FAILED,
        outcome=outcome,
        output={"message": message},
    )


def invalid_input(request: AgentRequest, error: Exception | str) -> AgentResult:
    """입력이 계약과 맞지 않을 때. 오류 상세는 짧게만 돌려주고 외부 문구를 그대로 싣지 않는다."""
    detail = str(error)
    return failed(request, "INVALID_INPUT", detail.splitlines()[0][:200] if detail else "invalid")


def gate_blocked(request: AgentRequest, decision: ExecuteDecision) -> AgentResult:
    """실행 관문이 실행을 허락하지 않았을 때(거부·검증 거절·보류)."""
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=f"gate_{decision.status}",
        output={
            "execution_id": decision.execution_id,
            "grade": decision.grade,
            "status": decision.status,
            "hold_reason": decision.hold_reason,
            "review_id": decision.review_id,
        },
    )


def with_key(meta: RequestMeta, suffix: str) -> RequestMeta:
    """관문 호출용 멱등성 키: 요청 키에 액션 이름을 붙여 액션마다 따로 중복을 막는다."""
    base = meta.idempotency_key or ""
    return meta.model_copy(update={"idempotency_key": f"{base}:{suffix}"})
