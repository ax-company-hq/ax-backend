"""hr 에이전트 skill 호출 (A2A). 목 모드가 아닐 때 인사 탭 API가 쓴다.

- 질문처럼 이미 사건이 있는 대상은 그 사건의 `context_id`를 그대로 쓴다 (`call(context_id=...)`).
  사건이 없는 화면 호출은 `support_client`와 같은 임시 접두사 `ctx_ui_`로 새 사건을 만든다.
- 상태를 바꾸는 skill의 멱등 키는 호출하는 쪽이 `key`로 주거나, `target`(대상)으로 만든다.
- 결과의 `status`·`outcome`을 API 오류로 바꾼다 (`unwrap`). 매핑표는 `docs/notes/hr/
  dashboard-api-contract.md` 5장과 같다. `input-required`는 오류가 아니라 사람이 할 일이 있다는
  뜻이라 출력을 그대로 돌려준다.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ax_agent_template import config
from ax_agent_template.client import call_agent
from ax_contracts.schemas import AgentRequest, AgentResult, RequestMeta, TaskStatus
from ax_dashboard_api.envelope import ApiError
from ax_dashboard_api.support_client import UI_CONTEXT_PREFIX

AGENT_NAME = "dashboard-api"
TARGET = "hr"
DEADLINE_SECONDS = 30

# hr가 정책상 거절할 때 `completed` + `outcome`으로 돌려주는 코드
NOT_FOUND_OUTCOMES = frozenset(
    {
        "UNKNOWN_CARD",
        "UNKNOWN_STEP",
        "UNKNOWN_QUESTION",
        "UNKNOWN_HANDOVER",
        "UNKNOWN_CHANGE",
        "UNKNOWN_MEMBER",
        "UNKNOWN_AREA",
    }
)
CONFLICT_OUTCOMES = frozenset(
    {
        "CONFLICT",
        "NOT_CANDIDATE",
        "BAD_STATE",
        "LOCKED",
        "LOCK_LOST",
        "EXPIRED",
        "CANCELLED",
        "HANDOVER_CLOSED",
    }
)

SendFn = Callable[[str, AgentRequest], Awaitable[AgentResult]]
SEND: SendFn = call_agent  # 테스트에서 바꿀 수 있다


def new_context_id() -> str:
    return f"{UI_CONTEXT_PREFIX}{uuid.uuid4().hex[:12]}"


def _message(result: AgentResult, default: str) -> str:
    return str(result.output.get("message", default)) if result.output else default


def unwrap(result: AgentResult) -> dict[str, Any]:
    """결과를 출력 dict로 바꾸거나 API 오류로 올린다."""
    outcome = str(result.outcome or "")
    if result.status == TaskStatus.REJECTED:
        if outcome == "INVALID_INPUT":
            raise ApiError(422, "INVALID_INPUT", _message(result, "입력이 형식과 다르다"))
        raise ApiError(403, "UPSTREAM_REJECTED", f"hr가 요청을 거부했다 ({outcome})")
    if result.status == TaskStatus.CANCELED:
        raise ApiError(504, "UPSTREAM_TIMEOUT", "hr 에이전트가 제때 응답하지 않았다")
    if result.status == TaskStatus.FAILED:
        raise ApiError(502, "UPSTREAM_ERROR", f"hr 호출 실패 ({outcome or 'failed'})")
    if outcome in NOT_FOUND_OUTCOMES:
        raise ApiError(404, "NOT_FOUND", _message(result, "찾을 수 없다"))
    if outcome in CONFLICT_OUTCOMES:
        raise ApiError(409, "CONFLICT", _message(result, "현재 상태에서는 할 수 없다"))
    if outcome.startswith("gate_"):
        state = outcome.removeprefix("gate_")
        raise ApiError(409, "NOT_EXECUTED", f"실행되지 않았다 ({state}). 승인 대기 또는 거부 상태")
    return result.output


async def call(
    skill: str,
    input: dict[str, Any] | None = None,
    *,
    context_id: str | None = None,
    key: str | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """hr skill을 부른다. 상태를 바꾸는 skill에는 `key`나 `target`으로 멱등 키를 준다."""
    context = context_id or new_context_id()
    config.set_agent_name(AGENT_NAME)
    request = AgentRequest(
        meta=RequestMeta(
            context_id=context,
            token=config.agent_token(AGENT_NAME),
            deadline=datetime.now(UTC) + timedelta(seconds=DEADLINE_SECONDS),
            idempotency_key=key or (f"{context}:{skill}:{target}" if target else None),
        ),
        skill=skill,
        input=input or {},
    )
    return unwrap(await SEND(TARGET, request))
