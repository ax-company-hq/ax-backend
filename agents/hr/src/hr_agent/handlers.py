"""hr skill 핸들러. 기능마다 모듈이 있고 여기서는 부품 묶음(`Services`)과 연결만 한다.

새 skill을 만들 때는 모듈에 `async def run(svc, request)`를 만들고 여기 `HANDLERS`에 등록한다.
상태를 바꾸는 일은 모두 `svc.gate.execute()`를 거치고, 외부 입력(`request.untrusted`)은 지시로
해석하지 않는다.
"""

from __future__ import annotations

from ax_contracts.schemas import AgentRequest, AgentResult
from hr_agent.cards import service as cards
from hr_agent.handover import service as handover
from hr_agent.onboarding import service as onboarding
from hr_agent.roster import service as roster
from hr_agent.services import get_services


async def record_case(request: AgentRequest) -> AgentResult:
    return await cards.record_case(get_services(), request)


async def get_roster(request: AgentRequest) -> AgentResult:
    return await roster.get_roster(get_services(), request)


async def start_handover(request: AgentRequest) -> AgentResult:
    return await handover.start_handover(get_services(), request)


async def answer_handover_question(request: AgentRequest) -> AgentResult:
    return await handover.answer_handover_question(get_services(), request)


async def confirm_handover(request: AgentRequest) -> AgentResult:
    return await handover.confirm_handover(get_services(), request)


async def start_onboarding(request: AgentRequest) -> AgentResult:
    return await onboarding.start_onboarding(get_services(), request)


HANDLERS = {
    "start_onboarding": start_onboarding,
    "record_case": record_case,
    "get_roster": get_roster,
    "start_handover": start_handover,
    "answer_handover_question": answer_handover_question,
    "confirm_handover": confirm_handover,
}
