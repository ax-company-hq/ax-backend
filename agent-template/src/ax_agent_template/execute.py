"""실행 관문 `execute()`: 상태를 바꾸는 모든 액션은 반드시 이 함수를 거친다.

처리 순서:

1. `ExecuteRequest`를 만들어 safety 서비스로 보낸다.
2. 응답이 없으면 거부한다(fail-closed).
   미등록 액션·스키마 오류·교차검증 거절·무응답은 실행하지 않는다.
3. safety가 등급을 판정한다: A는 즉시 실행, B는 검증자가 교차검증한 뒤 실행, C는 보류 큐에 들어간다.
4. 같은 `idempotency_key`의 두 번째 요청은 이전 결과를 돌려준다.

규칙:

- 외부 API를 직접 호출해 상태를 바꾸지 않는다. MCP **쓰기** 도구(`shop_issue_coupon`,
  `github_create_pull` 등)는 이 함수가 `executed`를 돌려준 뒤에만 호출하고, LLM 도구 목록에서 뺀다.
- `ExecuteRequest.claimed_grade`는 기록만 하고 판정에 쓰지 않는다. 금액은 safety가 다시 계산한다.

TODO: safety 서비스(`safety`, 포트 8001) 호출. 지금은 목 모드(`AGENT_TEMPLATE_MOCK=1`)에서만
`executed`(A)를 돌려주고, 그 밖에는 거부한다. 테스트에서 다른 결과가 필요하면 이 함수를 대체한다.
"""

from __future__ import annotations

import itertools

from ax_agent_template.config import get_agent_name, is_mock
from ax_contracts.schemas import (
    Evidence,
    ExecuteDecision,
    ExecuteRequest,
    RequestMeta,
)

_mock_counter = itertools.count(1)


async def _send_to_safety(request: ExecuteRequest) -> ExecuteDecision:
    """TODO: safety 서비스로 `ExecuteRequest`를 보내고 `ExecuteDecision`을 받는다."""
    if is_mock():
        return ExecuteDecision(
            execution_id=f"exe_mock_{next(_mock_counter)}", grade="A", status="executed"
        )
    raise NotImplementedError("TODO: safety 서비스 호출")


async def execute(
    action: str,
    params: dict,
    context: RequestMeta,
    evidence: Evidence,
) -> ExecuteDecision:
    """액션 하나의 실행을 요청하고 판정 결과를 돌려준다.

    `context.idempotency_key`는 필수다(`context_id + 액션 + 대상`).
    safety에 닿지 못하면 `rejected`를 돌려준다(fail-closed).
    """
    if not context.idempotency_key:
        raise ValueError("상태 변경에는 idempotency_key가 필요하다")

    request = ExecuteRequest(
        action=action,
        requester=get_agent_name(),
        context_id=context.context_id,
        tenant_id=context.tenant_id,
        idempotency_key=context.idempotency_key,
        params=params,
        evidence=evidence,
    )
    try:
        return await _send_to_safety(request)
    except Exception:
        return ExecuteDecision(execution_id="exe_unavailable", grade="C", status="rejected")
