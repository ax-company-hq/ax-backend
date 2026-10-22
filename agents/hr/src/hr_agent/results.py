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
    """실행 오류(외부 호출 실패 등). 정책상 거절에는 `refused`를 쓴다."""
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.FAILED,
        outcome=outcome,
        output={"message": message},
    )


def refused(request: AgentRequest, outcome: str, message: str) -> AgentResult:
    """정책상 거절(없는 대상·잠금·상태 불일치 등): 정상 처리한 결과라 `completed` + `outcome`."""
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome=outcome,
        output={"message": message},
        evidence=Evidence(data_summary=message, collected_at=None),
    )


def invalid_input(request: AgentRequest, error: Exception | str) -> AgentResult:
    """입력이 계약과 맞지 않을 때. 오류 상세는 짧게만 싣는다.

    계약의 `ErrorCode`에 입력 오류 값이 없어 `rejected`로 쓸 수 없다
    (계약 후속 제안: `INVALID_INPUT`).
    그때까지는 `failed` + `outcome="INVALID_INPUT"`이다.
    """
    detail = str(error)
    return failed(request, "INVALID_INPUT", detail.splitlines()[0][:200] if detail else "invalid")


def gate_blocked(request: AgentRequest, decision: ExecuteDecision) -> AgentResult:
    """실행 관문이 실행을 허락하지 않았을 때. 보류(C)는 사람 승인을 기다리는 `input-required`다."""
    held = decision.status == "held"
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.INPUT_REQUIRED if held else TaskStatus.COMPLETED,
        reason_code=InputReason.HUMAN_INPUT if held else None,
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
