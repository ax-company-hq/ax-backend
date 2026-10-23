"""변경 요청 결과 통지: planning → 요청 부서 `receive_change_result` (계약 #293, safety #304).

요청을 닫을 때(완료·기각) 같은 `ctx_req_` 사건으로 결과를 보낸다(path를 비운 새 요청, 합의).
목 모드에서는 보내지 않고(다른 부서가 떠 있지 않다) 서비스가 활동 기록만 남긴다.
실제 모드 실패는 요청을 닫는 일을 막지 않는다: 결과를 돌려받아 활동에 남긴다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from ax_agent_template import config as template_config
from ax_agent_template.client import call_agent
from ax_contracts.schemas import AgentRequest, RequestMeta, TaskStatus
from ax_contracts.schemas.planning import ChangeResultInput

SKILL = "receive_change_result"
# 이 skill을 여는 요청 부서 (#304). 다른 부서는 활동 기록만 남긴다
RECEIVERS = frozenset({"support", "hr", "data"})
CALL_TIMEOUT_SECONDS = 5

# (요청 부서, context_id, tenant_id, 결과) → 보낸 결과 한 줄 (활동 기록용)
ResultSender = Callable[[str, str, str, ChangeResultInput], Awaitable[str]]


def idempotency_key(context_id: str, request_id: str) -> str:
    return f"{context_id}:change_result:{request_id}"


async def send_change_result(
    dept: str, context_id: str, tenant_id: str, result: ChangeResultInput
) -> str:
    """요청 부서에 결과를 보낸다. 목 모드거나 받는 skill이 없는 부서면 보내지 않는다."""
    if dept not in RECEIVERS:
        return "기록만 (받는 skill 없음)"
    if template_config.is_mock("planning"):
        return "기록만 (목 모드)"
    meta = RequestMeta(
        context_id=context_id,
        tenant_id=tenant_id,
        token="replaced-by-call_agent",
        path=[],
        deadline=datetime.now(UTC) + timedelta(seconds=CALL_TIMEOUT_SECONDS),
        idempotency_key=idempotency_key(context_id, result.request_id),
    )
    out = await call_agent(
        dept, AgentRequest(meta=meta, skill=SKILL, input=result.model_dump(mode="json"))
    )
    if out.status == TaskStatus.COMPLETED:
        return f"보냄 (적용 {out.output.get('applied', '?')}건)"
    return f"보내지 못함 ({out.status.value} {out.outcome or ''})".strip()
