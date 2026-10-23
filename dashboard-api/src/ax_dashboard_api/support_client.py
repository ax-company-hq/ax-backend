"""support 에이전트 skill 호출 (A2A). 목 모드가 아닐 때 문의 API가 쓴다.

- 요청마다 새 사건 ID `ctx_ui_<hex>`를 붙인다 (`UI_CONTEXT_PREFIX`). 계약에 정해진 접두사는
  쓰지 않는다: `ctx_inc_`는 통합 에이전트, `ctx_req_`는 기획개발(변경 요청), `ctx_cmp_`는
  마케팅이 발급한다. 화면 호출용 접두사는 아직 계약에 없어 요청 이슈를 올려 뒀다 (README "사건 ID").
- 고객·상담원 원문은 `untrusted`로만 넘긴다.
- 결과의 `outcome`·`status`를 API 오류로 바꾼다 (`unwrap`).
- 상태를 바꾸는 skill은 support가 `execute()`를 거친다. 실행되지 않으면(보류·거부) 409로 알린다.
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

AGENT_NAME = "dashboard-api"
# 화면(상담원) 호출의 사건 ID 접두사. 계약 `ContextPrefix`에 없는 임시 값이라 이 상수 한 곳에서만
# 쓴다. 계약이 접두사를 정하면 여기만 바꾼다. 예약된 접두사와 겹치면 안 된다 (tests가 확인한다).
UI_CONTEXT_PREFIX = "ctx_ui_"
TARGET = "support"
DEADLINE_SECONDS = 30

SendFn = Callable[[str, AgentRequest], Awaitable[AgentResult]]
SEND: SendFn = call_agent  # 테스트에서 바꿀 수 있다


def _meta(context_id: str, key: str | None) -> RequestMeta:
    config.set_agent_name(AGENT_NAME)
    return RequestMeta(
        context_id=context_id,
        token=config.agent_token(AGENT_NAME),
        deadline=datetime.now(UTC) + timedelta(seconds=DEADLINE_SECONDS),
        idempotency_key=key,
    )


def unwrap(result: AgentResult) -> dict[str, Any]:
    """결과를 출력 dict로 바꾸거나 API 오류로 올린다."""
    outcome = str(result.outcome or "")
    if result.status == TaskStatus.COMPLETED and not outcome:
        return result.output
    if outcome == "not_found":
        raise ApiError(404, "NOT_FOUND", str(result.output.get("message", "찾을 수 없다")))
    if outcome == "blocked_by_guard":
        findings = result.output.get("validation", {}).get("findings", [])
        message = findings[0]["message"] if findings else "답변 가드에 걸렸다"
        raise ApiError(422, "REPLY_BLOCKED", message)
    if outcome == "invalid_state":
        raise ApiError(
            409, "INVALID_STATE", str(result.output.get("message", "현재 상태에서는 할 수 없다"))
        )
    if outcome.startswith("not_executed_"):
        state = outcome.removeprefix("not_executed_")
        raise ApiError(409, "NOT_EXECUTED", f"실행되지 않았다 ({state}). 승인 대기 또는 거부 상태")
    if result.status == TaskStatus.CANCELED:
        raise ApiError(504, "UPSTREAM_TIMEOUT", "support 에이전트가 제때 응답하지 않았다")
    if result.status == TaskStatus.REJECTED:
        raise ApiError(403, "UPSTREAM_REJECTED", f"support가 요청을 거부했다 ({outcome})")
    if result.status == TaskStatus.COMPLETED:
        return result.output
    raise ApiError(502, "UPSTREAM_ERROR", f"support 호출 실패 ({outcome or result.status.value})")


async def call(
    skill: str,
    input: dict[str, Any] | None = None,
    untrusted: dict[str, Any] | None = None,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    """support skill을 부른다. 상태를 바꾸는 skill에는 `target`(대상)을 줘 멱등 키를 만든다."""
    context_id = f"{UI_CONTEXT_PREFIX}{uuid.uuid4().hex[:12]}"
    key = f"{context_id}:{skill}:{target}" if target else None
    request = AgentRequest(
        meta=_meta(context_id, key),
        skill=skill,
        input=input or {},
        untrusted=untrusted or {},
    )
    return unwrap(await SEND(TARGET, request))
