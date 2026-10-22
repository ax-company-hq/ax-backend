"""planning 에이전트에 `find_related_prs`를 묻는 클라이언트 (이슈 #52).

장애 구간 앞뒤에 병합된 PR 후보를 받아 시간 관계만 근거(Evidence)로 만든다.
원인은 판정하지 않는다. 응답이 없거나 실패하면 None을 돌려주고, 호출한 쪽은 이 근거 없이 진행한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from ax_agent_template.client import call_agent
from ax_contracts.schemas import AgentRequest, AgentResult, Evidence, RequestMeta, TaskStatus
from ax_contracts.schemas.planning import FindRelatedPrsInput, FindRelatedPrsOutput

PLANNING_AGENT = "planning"
SKILL_ID = "find_related_prs"
# 보조 근거라서 오래 기다리지 않는다
DEFAULT_TIMEOUT_SECONDS = 5.0

SendFn = Callable[[str, AgentRequest], Awaitable[AgentResult]]


async def find_related_prs(
    window_from: datetime,
    window_to: datetime,
    component: str | None,
    meta: RequestMeta,
    *,
    send: SendFn = call_agent,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FindRelatedPrsOutput | None:
    """planning에 관련 PR 후보를 묻는다. 타임아웃·실패·거부면 None."""
    short_deadline = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
    request_meta = meta.model_copy(update={"deadline": min(meta.deadline, short_deadline)})
    payload = FindRelatedPrsInput(window_from=window_from, window_to=window_to, component=component)
    request = AgentRequest(meta=request_meta, skill=SKILL_ID, input=payload.model_dump(mode="json"))
    try:
        result = await asyncio.wait_for(send(PLANNING_AGENT, request), timeout=timeout_seconds)
    except Exception:  # 보조 질의라 어떤 실패든 근거 없이 진행한다
        return None
    if result.status != TaskStatus.COMPLETED:
        return None
    try:
        return FindRelatedPrsOutput.model_validate(result.output)
    except ValidationError:
        return None


def prs_to_evidence(output: FindRelatedPrsOutput) -> Evidence:
    """PR 후보를 시간 관계만 담은 근거로 바꾼다. 인과 표현은 쓰지 않는다."""
    if not output.candidates:
        return Evidence(data_summary="장애 구간 앞의 병합 PR 후보 없음")
    lines = [
        f"PR #{pr.pr_number}(병합 {pr.merged_at.strftime('%Y-%m-%dT%H:%M:%SZ')}) 이후 구간"
        for pr in output.candidates
    ]
    return Evidence(
        queries=[f"planning.{SKILL_ID}"],
        data_summary="; ".join(lines),
        collected_at=datetime.now(UTC),
        ref_ids=[f"pr:{pr.pr_number}" for pr in output.candidates],
    )
