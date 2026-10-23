"""hr skill 핸들러.

- `record_case`는 v0 스텁이다 (입력을 그대로 돌려준다).
- 인수인계(`start_handover`, `answer_handover_question`)는 메모리 저장소로 동작한다 (영속 저장은 TODO).
  완성도·빈 자리 수는 코드가 계산한다. 인계자의 답변은 데이터로만 저장하고 지시로 해석하지 않는다.
  다른 에이전트에 넘길 때는 `untrusted`로 표시해야 한다.
- 질문 대기 상태는 질문의 `context_id`에 묶어 저장한다. 답변 요청은 그 `context_id`를 그대로 쓴다.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    Evidence,
    InputReason,
    TaskStatus,
)
from ax_contracts.schemas.hr import (
    AnswerHandoverQuestionInput,
    AnswerHandoverQuestionOutput,
    HandoverQuestion,
    StartHandoverInput,
    StartHandoverOutput,
)

# 인계자에게 확인할 빈 자리 필드와 질문 문구
_GAP_QUESTIONS = {
    "successor_id": "인수자는 누구인가요?",
    "area": "인계할 담당 영역은 무엇인가요?",
}


class _Handover:
    def __init__(self, handover_id: str, context_id: str, total_gaps: int) -> None:
        self.handover_id = handover_id
        self.context_id = context_id
        self.total_gaps = total_gaps
        self.questions: dict[str, HandoverQuestion] = {}
        self.answers: dict[str, str] = {}  # question_id → 인계자 답변 (데이터로만 보관)


class State:
    def __init__(self) -> None:
        self.handovers: dict[str, _Handover] = {}
        self.question_owner: dict[str, str] = {}  # question_id → handover_id
        self.answered: dict[str, dict[str, Any]] = {}  # idempotency_key → 첫 결과 output


STATE = State()


def reset_state() -> None:
    """테스트 사이에 저장소를 비운다."""
    global STATE
    STATE = State()


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def _evidence(summary: str, ref_ids: list[str]) -> Evidence:
    return Evidence(
        queries=["hr.handover.store"],
        data_summary=summary,
        collected_at=datetime.now(UTC),
        ref_ids=ref_ids,
    )


def _invalid(request: AgentRequest, error: ValidationError) -> AgentResult:
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.FAILED,
        outcome="invalid_input",
        output={"errors": [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in error.errors()]},
    )


def _parse[T: BaseModel](request: AgentRequest, model: type[T]) -> T | AgentResult:
    try:
        return model.model_validate(request.input)
    except ValidationError as error:
        return _invalid(request, error)


def _completeness(handover: _Handover) -> int:
    if handover.total_gaps == 0:
        return 100
    filled = handover.total_gaps - len(handover.questions)
    return filled * 100 // handover.total_gaps


def _waiting_result(
    request: AgentRequest, handover: _Handover, output: BaseModel, summary: str
) -> AgentResult:
    evidence = _evidence(summary, [handover.handover_id])
    if handover.questions:
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.INPUT_REQUIRED,
            reason_code=InputReason.HUMAN_INPUT,
            outcome="waiting_for_answer",
            output=output.model_dump(mode="json"),
            evidence=evidence,
        )
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        outcome="ready",
        output=output.model_dump(mode="json"),
        evidence=evidence,
    )


async def record_case(request: AgentRequest) -> AgentResult:
    """TODO: `RecordCase`로 검증 → 결정 탐지 → 카드 생성 → `memory_write_card`(execute 경유)."""
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        output=dict(request.input),
        decision_reason=request.input.get("decision_reason"),
        alternatives_considered=list(request.input.get("alternatives_considered", [])),
    )


async def start_handover(request: AgentRequest) -> AgentResult:
    """인수인계 초안을 만들고 빈 자리마다 질문을 낸다. TODO: 인계 항목은 planning·카드에서 수집."""
    parsed = _parse(request, StartHandoverInput)
    if isinstance(parsed, AgentResult):
        return parsed
    context_id = request.meta.context_id
    handover_id = f"ho_{_digest(context_id, parsed.owner_id)}"
    handover = STATE.handovers.get(handover_id)
    if handover is None:  # 같은 사건·인계자의 재요청은 기존 초안을 돌려준다
        gaps = [field for field in _GAP_QUESTIONS if getattr(parsed, field) is None]
        handover = _Handover(handover_id, context_id, len(gaps))
        for field in gaps:
            question_id = f"q_{_digest(handover_id, field)}"
            handover.questions[question_id] = HandoverQuestion(
                question_id=question_id,
                context_id=context_id,
                assignee_id=parsed.owner_id,
                question=_GAP_QUESTIONS[field],
                gap_field=field,
            )
            STATE.question_owner[question_id] = handover_id
        STATE.handovers[handover_id] = handover
    questions = list(handover.questions.values())
    output = StartHandoverOutput(
        handover_id=handover_id,
        status="input_required" if questions else "ready",
        completeness=_completeness(handover),
        gap_count=len(questions),
        questions=questions,
    )
    return _waiting_result(request, handover, output, f"빈 자리 {len(questions)}건")


async def answer_handover_question(request: AgentRequest) -> AgentResult:
    """인계자의 답변을 반영한다. 같은 `idempotency_key`는 한 번만 반영한다."""
    parsed = _parse(request, AnswerHandoverQuestionInput)
    if isinstance(parsed, AgentResult):
        return parsed
    key = request.meta.idempotency_key
    if key is not None and key in STATE.answered:
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.COMPLETED,
            outcome="duplicate",
            output=STATE.answered[key],
            evidence=_evidence("이미 반영된 답변", [parsed.question_id]),
        )
    handover_id = STATE.question_owner.get(parsed.question_id)
    handover = STATE.handovers.get(handover_id) if handover_id else None
    question = handover.questions.get(parsed.question_id) if handover else None
    if handover is None or question is None or question.context_id != request.meta.context_id:
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.FAILED,
            outcome="question_not_found",
            output={"question_id": parsed.question_id},
        )
    handover.answers[parsed.question_id] = parsed.answer
    del handover.questions[parsed.question_id]
    questions = list(handover.questions.values())
    output = AnswerHandoverQuestionOutput(
        handover_id=handover.handover_id,
        status="input_required" if questions else "ready",
        completeness=_completeness(handover),
        remaining_questions=questions,
    )
    if key is not None:
        STATE.answered[key] = output.model_dump(mode="json")
    return _waiting_result(request, handover, output, f"남은 질문 {len(questions)}건")


HANDLERS = {
    "record_case": record_case,
    "start_handover": start_handover,
    "answer_handover_question": answer_handover_question,
}
