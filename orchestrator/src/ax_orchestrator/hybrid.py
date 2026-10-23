"""혼합 모드: 지정한 부서만 실제 에이전트를 부르고 나머지는 가짜 부서가 대신한다.

`ORCHESTRATOR_REAL_DEPTS=hr,finance`처럼 쉼표로 부서 이름을 준다. 실제 부서의 주소는
`AX_<부서>_URL`(없으면 agent-template 기본 포트 표), 토큰은 총괄의 `ORCHESTRATOR_TOKEN`
(`ORCHESTRATOR_MOCK=1`이면 `mock:orchestrator`)이다.
각 팀은 자기 에이전트만 띄우고 이 변수에 자기 이름을 넣으면 총괄 목업에 붙여 볼 수 있다.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from ax_contracts.schemas import AgentRequest, AgentResult
from ax_orchestrator.mock.fake_departments import FakeDepartments
from ax_orchestrator.real import call_agent, http_card_fetcher
from ax_orchestrator.registry import CardFetcher

RealCaller = Callable[[str, AgentRequest], Awaitable[AgentResult]]


def parse_real_depts(value: str | None, known: list[str]) -> set[str]:
    """환경변수 값을 부서 집합으로. 모르는 이름은 조용히 무시하지 않고 오류로 알린다."""
    names = {n.strip() for n in (value or "").split(",") if n.strip()}
    unknown = names - set(known)
    if unknown:
        raise ValueError(
            f"알 수 없는 부서: {', '.join(sorted(unknown))} (가능: {', '.join(known)})"
        )
    return names


async def _call_real(dept: str, req: AgentRequest) -> AgentResult:
    return await call_agent(dept, req)


class HybridDepartments:
    """`Engine(caller=...)`과 `Registry.load(fetch=...)`에 쓰는 호출기·카드 수집기."""

    def __init__(
        self,
        fakes: FakeDepartments,
        real_depts: set[str],
        *,
        real_caller: RealCaller | None = None,
        real_fetch: CardFetcher | None = None,
    ) -> None:
        self.fakes = fakes
        self.real = real_depts
        self._real_caller = real_caller or _call_real
        self._real_fetch = real_fetch or http_card_fetcher()

    async def fetch_card(self, dept: str) -> dict[str, Any] | None:
        if dept in self.real:
            return await self._real_fetch(dept)
        return await self.fakes.fetch_card(dept)

    async def __call__(self, dept: str, req: AgentRequest) -> AgentResult:
        if dept in self.real:
            return await self._real_caller(dept, req)
        return await self.fakes(dept, req)

    def modes(self, depts: list[str]) -> dict[str, str]:
        return {d: ("real" if d in self.real else "mock") for d in depts}


def real_depts_from_env(known: list[str]) -> set[str]:
    return parse_real_depts(os.getenv("ORCHESTRATOR_REAL_DEPTS"), known)
