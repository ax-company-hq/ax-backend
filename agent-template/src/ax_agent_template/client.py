"""다른 에이전트 호출 클라이언트.

`call_agent(target, req)`:

1. 호출 전에 `path`에 자기 이름을 붙이고, 토큰은 자기 에이전트 토큰으로 바꾼다
   (받는 쪽은 토큰에서 요청자를 꺼낸다).
2. `deadline`이 이미 지났으면 호출하지 않고 `canceled`(`TIMEOUT`)를 돌려준다.
3. 마감까지 응답이 없으면 포기하고 `canceled`(`TIMEOUT`), 연결·응답 오류는 `failed`로 돌려준다.

`target`은 에이전트 이름(`planning`)이거나 주소(`http://localhost:8101`)다.
TODO: 이름 → 주소는 지금 포트 표와 환경변수(`AX_<이름>_URL`)로 찾는다.
나중에 Agent Card 레지스트리로 바꾼다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from ax_agent_template.config import agent_token, agent_url, get_agent_name
from ax_agent_template.server import error_result
from ax_contracts.schemas import AgentRequest, AgentResult, ErrorCode, TaskStatus


async def call_agent(
    target: str,
    req: AgentRequest,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> AgentResult:
    """대상 에이전트에 요청을 보내고 결과를 받는다. `http_client`는 테스트용으로 바꿀 수 있다."""
    name = get_agent_name()
    remaining = (req.meta.deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return error_result(req.meta, TaskStatus.CANCELED, ErrorCode.TIMEOUT)

    meta = req.meta.model_copy(update={"path": [*req.meta.path, name], "token": agent_token(name)})
    sent = req.model_copy(update={"meta": meta})
    url = f"{agent_url(target)}/a2a"

    client = http_client or httpx.AsyncClient()
    try:
        response = await client.post(url, json=sent.model_dump(mode="json"), timeout=remaining)
    except httpx.TimeoutException:
        return error_result(meta, TaskStatus.CANCELED, ErrorCode.TIMEOUT)
    except httpx.HTTPError:
        return error_result(meta, TaskStatus.FAILED, "unreachable")
    finally:
        if http_client is None:
            await client.aclose()

    if response.status_code != 200:
        return error_result(meta, TaskStatus.FAILED, f"http_{response.status_code}")
    try:
        return AgentResult.model_validate(response.json())
    except (ValidationError, ValueError):
        return error_result(meta, TaskStatus.FAILED, ErrorCode.INVALID_OUTPUT)
