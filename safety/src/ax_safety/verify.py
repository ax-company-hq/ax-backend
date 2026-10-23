"""B 교차검증 호출과 safety 이벤트 발행 (기본 구현).

- 교차검증: 레지스트리의 검증자 skill을 A2A(`call_agent`)로 부른다. 요청 부서는 검증자를 직접
  부르지 않는다(공통 구성안 13장 후속 결정 13). 결과는 `completed`이고 출력이
  `decision == "approved"` 또는 `approved == true`일 때만 승인이다. 그 밖의 완료는 거절,
  마감 초과·실패·거부는 무응답으로 본다.
- 이벤트: agent-template `publish`로만 낸다. data 모델이 계약에 들어오기 전까지는 dict다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from ax_agent_template import call_agent, publish
from ax_contracts.schemas import (
    EVENT_DATA_MODELS,
    AgentRequest,
    EventEnvelope,
    EventType,
    ExecuteRequest,
    RequestMeta,
    TaskStatus,
)
from ax_safety.gate import (
    ReviewOutcome,
    ReviewTask,
    ThresholdReviewTask,
    VerifyOutcome,
    VerifyTask,
)
from ax_safety.store import Review


async def call_verifier(task: VerifyTask) -> VerifyOutcome:
    request = AgentRequest(
        meta=RequestMeta(
            context_id=task.context_id,
            tenant_id=task.tenant_id,
            token="",  # call_agent가 safety 토큰으로 바꾼다
            path=[],
            deadline=datetime.now(UTC) + timedelta(seconds=task.timeout_seconds),
            idempotency_key=f"verify:{task.execution_id}",
        ),
        skill=task.skill,
        input=task.input,
    )
    result = await call_agent(task.verifier, request)
    detail: dict[str, Any] = {"status": result.status.value, "outcome": result.outcome}
    if result.status != TaskStatus.COMPLETED:
        return VerifyOutcome("unavailable", detail)
    output = result.output
    detail["reason_code"] = output.get("reason_code")
    approved = output.get("decision") == "approved" or output.get("approved") is True
    return VerifyOutcome("approved" if approved else "rejected", detail)


async def call_reviewer(task: ReviewTask) -> ReviewOutcome:
    """재무 AI의 `review_hold`에 C 건 1차 판단을 묻는다. 완료가 아니거나 모르는 답이면 사람에게."""
    request = AgentRequest(
        meta=RequestMeta(
            context_id=task.context_id,
            tenant_id=task.tenant_id,
            token="",  # call_agent가 safety 토큰으로 바꾼다
            path=[],
            deadline=datetime.now(UTC) + timedelta(seconds=task.timeout_seconds),
            idempotency_key=f"review:{task.review_id}",
        ),
        skill=task.skill,
        input=task.input,
    )
    result = await call_agent(task.reviewer, request)
    if result.status != TaskStatus.COMPLETED:
        return ReviewOutcome("escalate", f"reviewer_{result.status.value}")
    out = result.output
    decision = out.get("decision")
    if decision not in ("approve", "reject", "escalate"):
        return ReviewOutcome("escalate", "reviewer_invalid_output")
    return ReviewOutcome(decision, str(out.get("reason_code", "")), str(out.get("explanation", "")))


async def call_threshold_reviewer(task: ThresholdReviewTask) -> ReviewOutcome:
    """재무 AI의 `review_threshold_change`에 기준 바꾸기를 묻는다.

    요청한 사람이 쓴 이유는 `untrusted`로만 넘긴다. 완료가 아니거나 모르는 답이면 사람에게.
    """
    request = AgentRequest(
        meta=RequestMeta(
            context_id=task.context_id,
            tenant_id=task.tenant_id,
            token="",  # call_agent가 safety 토큰으로 바꾼다
            path=[],
            deadline=datetime.now(UTC) + timedelta(seconds=task.timeout_seconds),
            idempotency_key=f"threshold:{task.change_id}",
        ),
        skill=task.skill,
        input=task.input,
        untrusted=task.untrusted,
    )
    result = await call_agent(task.reviewer, request)
    if result.status != TaskStatus.COMPLETED:
        return ReviewOutcome("escalate", f"reviewer_{result.status.value}")
    out = result.output
    decision = out.get("decision")
    if decision not in ("approve", "reject", "escalate"):
        return ReviewOutcome("escalate", "reviewer_invalid_output")
    return ReviewOutcome(decision, str(out.get("reason_code", "")), str(out.get("explanation", "")))


async def publish_safety_event(
    event_type: str, source: ExecuteRequest | Review, data: dict[str, Any]
) -> None:
    key_id = data.get("review_id") if event_type == "safety.review_decided" else None
    key_id = key_id or data["execution_id"]
    envelope = EventEnvelope(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        type=event_type,
        occurred_at=datetime.now(UTC),
        tenant_id=source.tenant_id,
        context_id=source.context_id,
        source="safety",
        idempotency_key=f"{event_type}:{key_id}",
        data=_event_data(event_type, data),
    )
    await publish(envelope)


def _event_data(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """contracts의 safety 이벤트 data 모델(#92)로 검증한다. 형식이 틀리면 발행하지 않는다."""
    plain = {k: (v.value if hasattr(v, "value") else v) for k, v in data.items()}
    model = EVENT_DATA_MODELS.get(EventType(event_type))
    return model.model_validate(plain).model_dump(mode="json") if model else plain
