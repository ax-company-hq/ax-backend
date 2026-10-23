"""hr skill 핸들러. 기능마다 모듈이 있고 여기서는 부품 묶음(`Services`)과 연결만 한다.

새 skill을 만들 때는 모듈에 `async def run(svc, request)`를 만들고 여기 `HANDLERS`에 등록한다.
상태를 바꾸는 일은 모두 `svc.gate.execute()`를 거치고, 외부 입력(`request.untrusted`)은 지시로
해석하지 않는다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ax_contracts.schemas import AgentRequest, AgentResult
from hr_agent.assignment import service as assignment
from hr_agent.cards import decision as card_decision
from hr_agent.cards import service as cards
from hr_agent.clients.errors import UpstreamError, bind_meta
from hr_agent.handover import service as handover
from hr_agent.onboarding import service as onboarding
from hr_agent.planning_check import service as planning_check
from hr_agent.results import failed
from hr_agent.roster import service as roster
from hr_agent.services import get_services
from hr_agent.views import service as views


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


async def request_assignment_change(request: AgentRequest) -> AgentResult:
    return await assignment.request_assignment_change(get_services(), request)


async def confirm_reassignment(request: AgentRequest) -> AgentResult:
    return await assignment.confirm_reassignment(get_services(), request)


async def confirm_offboarding(request: AgentRequest) -> AgentResult:
    return await assignment.confirm_offboarding(get_services(), request)


async def check_plan(request: AgentRequest) -> AgentResult:
    return await planning_check.check_plan(get_services(), request)


Handler = Callable[[AgentRequest], Awaitable[AgentResult]]


def _guarded(fn: Handler) -> Handler:
    """요청 메타를 연동 클라이언트에 알리고, 다른 서비스가 실패하면 지어내지 않고 실패로 답한다."""

    async def run(request: AgentRequest) -> AgentResult:
        bind_meta(request.meta)
        try:
            return await fn(request)
        except UpstreamError as exc:
            return failed(
                request, "UPSTREAM_UNAVAILABLE", f"{exc.service} 연결 문제로 처리하지 못했다"
            )

    return run


def _with_services(fn):  # type: ignore[no-untyped-def]
    async def run(request: AgentRequest) -> AgentResult:
        return await fn(get_services(), request)

    return run


_HANDLERS = {
    # 대시보드 인사 화면용 조회와 사람 입력(온보딩 단계 완료, 후보 카드 승인·거절)
    "list_handovers": _with_services(views.list_handovers),
    "list_open_questions": _with_services(views.list_open_questions),
    "list_onboardings": _with_services(views.list_onboardings),
    "list_changes": _with_services(views.list_changes),
    "list_candidate_cards": _with_services(views.list_candidate_cards),
    "get_audit_timeline": _with_services(views.get_audit_timeline),
    "update_onboarding_step": _with_services(onboarding.update_onboarding_step),
    "decide_candidate_card": _with_services(card_decision.decide_candidate_card),
    "check_plan": check_plan,
    "request_assignment_change": request_assignment_change,
    "confirm_reassignment": confirm_reassignment,
    "confirm_offboarding": confirm_offboarding,
    "start_onboarding": start_onboarding,
    "record_case": record_case,
    "get_roster": get_roster,
    "start_handover": start_handover,
    "answer_handover_question": answer_handover_question,
    "confirm_handover": confirm_handover,
}


HANDLERS = {name: _guarded(fn) for name, fn in _HANDLERS.items()}
