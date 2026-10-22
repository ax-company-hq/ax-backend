"""hr skill 핸들러. 기능마다 모듈이 있고 여기서는 부품 묶음(`Services`)과 연결만 한다.

새 skill을 만들 때는 모듈에 `async def run(svc, request)`를 만들고 여기 `HANDLERS`에 등록한다.
상태를 바꾸는 일은 모두 `svc.gate.execute()`를 거치고, 외부 입력(`request.untrusted`)은 지시로
해석하지 않는다.
"""

from __future__ import annotations

from ax_contracts.schemas import AgentRequest, AgentResult
from hr_agent.cards import service as cards
from hr_agent.services import get_services


async def record_case(request: AgentRequest) -> AgentResult:
    return await cards.record_case(get_services(), request)


HANDLERS = {"record_case": record_case}
