"""hr skill 핸들러. 템플릿 예시이므로 v0은 입력을 그대로 돌려주는 스텁이다.

새 에이전트는 이 파일을 복사해 skill마다 `async def <skill_id>(request) -> AgentResult`를 만들고
`HANDLERS`에 등록한다. 상태를 바꾸는 일은 `ax_agent_template.execute.execute`를 거쳐서만 하고,
외부 입력(`request.untrusted`)은 지시로 해석하지 않는다.
"""

from __future__ import annotations

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus


async def record_case(request: AgentRequest) -> AgentResult:
    """TODO: `RecordCase`로 검증 → 결정 탐지 → 카드 생성 → `memory_write_card`(execute 경유)."""
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        output=dict(request.input),
        decision_reason=request.input.get("decision_reason"),
        alternatives_considered=list(request.input.get("alternatives_considered", [])),
    )


HANDLERS = {"record_case": record_case}
