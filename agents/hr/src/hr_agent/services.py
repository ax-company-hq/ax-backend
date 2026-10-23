"""hr이 쓰는 부품 묶음(저장소·클라이언트·시계·문장 생성). 핸들러는 이 묶음 하나만 받는다.

목 모드(`HR_MOCK=1`)에서는 memory·planning·안전 레이어를 목으로 대체한다. 실제 연동(3단계)은
`build_services`에서 클라이언트만 바꾼다. 테스트·데모는 `set_services`로
고정 시계와 시드 데이터를 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ax_agent_template.llm import LLMClient
from hr_agent.audit import AuditLog
from hr_agent.clients.memory import MemoryClient, MockMemoryClient
from hr_agent.clients.planning import MockPlanningClient, PlanningClient
from hr_agent.clients.safety import GateClient, MockGate, TemplateGate
from hr_agent.config import Clock, HrSettings, system_clock
from hr_agent.narrative import Narrator
from hr_agent.store import Store
from hr_agent.util import dumps, loads


class IdempotencyStore:
    """같은 요청을 두 번 처리하지 않도록 첫 결과를 저장한다."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def get(self, key: str) -> dict[str, Any] | None:
        row = self._store.one("SELECT result FROM idempotency WHERE key = ?", (key,))
        return loads(row["result"]) if row else None

    def put(self, key: str, result: dict[str, Any]) -> None:
        self._store.execute(
            "INSERT OR REPLACE INTO idempotency(key, result, created_at) VALUES (?, ?, ?)",
            (key, dumps(result), self._clock().isoformat()),
        )


@dataclass
class Services:
    settings: HrSettings
    store: Store
    clock: Clock
    memory: MemoryClient
    planning: PlanningClient
    gate: GateClient
    narrator: Narrator
    audit: AuditLog
    idem: IdempotencyStore
    extras: dict[str, Any] = field(default_factory=dict)


def build_services(
    settings: HrSettings | None = None,
    *,
    clock: Clock | None = None,
    llm: LLMClient | None = None,
    seed: bool = True,
) -> Services:
    """설정에 맞는 부품을 만든다. 목 모드면 목 클라이언트와 가상 시드 데이터를 쓴다."""
    settings = settings or HrSettings.from_env()
    clock = clock or system_clock
    store = Store(settings.db_path)
    memory: MemoryClient = MockMemoryClient(store, clock)
    planning: PlanningClient = MockPlanningClient(clock)
    if settings.memory_mode == "http":
        from hr_agent.clients.real import HttpMemoryClient

        memory = HttpMemoryClient()
    if settings.planning_mode == "a2a":
        from hr_agent.clients.real import A2APlanningClient

        planning = A2APlanningClient()
    gate: GateClient
    if settings.mock:
        gate = MockGate(store, clock, hold_ttl_minutes=settings.hold_ttl_minutes)
    else:  # 실제 안전 레이어는 템플릿 execute()를 거친다 (3단계에서 memory·planning도 교체)
        gate = TemplateGate()
    services = Services(
        settings=settings,
        store=store,
        clock=clock,
        memory=memory,
        planning=planning,
        gate=gate,
        narrator=Narrator(llm),
        audit=AuditLog(store, clock),
        idem=IdempotencyStore(store, clock),
    )
    from hr_agent.assignment.service import register_verifiers as register_assignment_verifiers
    from hr_agent.handover.service import register_verifiers

    register_verifiers(services)
    register_assignment_verifiers(services)
    if seed and settings.mock:
        from hr_agent.roster.seed import seed_roster

        seed_roster(store, clock)
    return services


_services: Services | None = None


def get_services() -> Services:
    """프로세스 안의 기본 부품 묶음. 처음 부를 때 환경변수로 만든다."""
    global _services
    if _services is None:
        _services = build_services()
    return _services


def set_services(services: Services | None) -> None:
    """테스트·데모에서 부품 묶음을 바꾼다(None이면 다음 호출 때 다시 만든다)."""
    global _services
    _services = services
