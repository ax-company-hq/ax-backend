"""테스트·목 모드 공용 하네스."""

from ax_orchestrator.engine import Engine
from ax_orchestrator.mock.fake_departments import FakeDepartments
from ax_orchestrator.models import Incident
from ax_orchestrator.registry import Registry

DEPTS = ["infra", "data", "planning", "marketing", "support", "finance", "hr"]


class Harness:
    def __init__(self, hold_timeout: float = 0.2) -> None:
        self.fakes = FakeDepartments()
        self.registry = Registry()
        self.events: list[str] = []
        self.hold_timeout = hold_timeout
        self.engine: Engine | None = None

    async def publish(self, kind: str, inc: Incident) -> None:
        self.events.append(kind)

    async def start(self, fetch=None) -> Engine:
        await self.registry.load(DEPTS, fetch or self.fakes.fetch_card)
        self.engine = Engine(
            self.registry,
            self.fakes,
            publish=self.publish,
            hold_timeout=self.hold_timeout,
        )
        return self.engine


def new_incident() -> Incident:
    return Incident(Incident.new_context_id(), "scenario_a", "payment 장애", "payment")
