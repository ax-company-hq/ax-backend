"""이벤트 수신과 사건 판단 (ORC-02·03).

이벤트를 받아 `event_id` 중복을 거르고, 사건이면 `ctx_inc_`를 발급해 plan을 돌린다.
같은 구성요소의 열린 사건이 있으면 새로 열지 않고 합친다.
판단은 규칙(코드)이 한다. 원인·분석 문장은 만들지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ax_contracts.schemas import EventEnvelope, EventType
from ax_orchestrator.engine import Engine
from ax_orchestrator.models import Incident, Plan, iso

LOOKBACK = timedelta(minutes=15)  # 사건이 열리기 직전 실패 주문·문의도 같은 사건으로 본다
INCIDENT_EVENTS = {EventType.SHOP_DEGRADED, EventType.INFRA_ANOMALY_DETECTED}
SEVERITY_OPENS = {"high", "critical"}
SUPPORT_EVENTS = {EventType.SUPPORT_INQUIRY_RECEIVED, EventType.SUPPORT_INQUIRY_SURGE}

SUBSCRIBED = [
    EventType.SHOP_DEGRADED,
    EventType.SHOP_RECOVERED,
    EventType.INFRA_ANOMALY_DETECTED,
    EventType.METRIC_ANOMALY,
    EventType.SUPPORT_INQUIRY_RECEIVED,
    EventType.SUPPORT_INQUIRY_SURGE,
]


class IncidentManager:
    def __init__(self, engine: Engine, plan_factory: Callable[[], Plan]) -> None:
        self.engine = engine
        self.plan_factory = plan_factory
        self.incidents: dict[str, Incident] = {}
        self._seen: set[str] = set()
        self._tasks: dict[str, asyncio.Task[Incident]] = {}

    def judge(self, env: EventEnvelope) -> str:
        """`open` | `note` | `ignore`."""
        if env.type in INCIDENT_EVENTS:
            return "open"
        if env.type == EventType.METRIC_ANOMALY:
            severity = str(env.data.get("severity", "")).lower()
            return "open" if severity in SEVERITY_OPENS else "note"
        if env.type in SUPPORT_EVENTS or env.type == EventType.SHOP_RECOVERED:
            return "note"
        return "ignore"

    def open_for(self, component: str) -> Incident | None:
        return next(
            (i for i in self.incidents.values() if i.status == "open" and i.component == component),
            None,
        )

    def find_active(self, component: str | None, occurred_at: datetime | None) -> Incident | None:
        """문의·주문 시각에 해당하는 사건 하나를 찾는다 (`find_active_incident`, #272).

        시각이 없으면 열린 사건만 본다. 시각이 있으면 사건 구간(시작 15분 전 ~ 종료)에
        들어오는 사건을 본다. 열린 사건이 먼저이고, 그다음은 최근에 열린 순서다.
        """
        found = [
            inc
            for inc in self.incidents.values()
            if (component is None or inc.component == component) and self._covers(inc, occurred_at)
        ]
        found.sort(key=lambda inc: (inc.status != "open", -inc.opened_at.timestamp()))
        return found[0] if found else None

    @staticmethod
    def _covers(inc: Incident, occurred_at: datetime | None) -> bool:
        if occurred_at is None:
            return inc.status == "open"
        if occurred_at < inc.opened_at - LOOKBACK:
            return False
        return inc.closed_at is None or occurred_at <= inc.closed_at

    async def handle(self, env: EventEnvelope) -> Incident | None:
        """이벤트 하나를 처리한다. 새로 연 사건 또는 합쳐진 사건을 돌려준다."""
        _, inc = await self.process(env)
        return inc

    async def process(self, env: EventEnvelope) -> tuple[str, Incident | None]:
        """처리 결과를 `(outcome, 사건)`으로 돌려준다.

        outcome: `duplicate`(같은 event_id) · `ignored`(총괄 소관 아님) ·
        `noted`(기록만, 열린 사건이 있으면 거기에) · `merged`(열린 사건에 합침) · `opened`(새 사건).
        """
        if env.event_id in self._seen:
            return "duplicate", None
        self._seen.add(env.event_id)
        verdict = self.judge(env)
        component = str(env.data.get("component") or "api")
        existing = self.open_for(component)
        if verdict == "ignore":
            return "ignored", None
        if verdict == "note":
            if existing is not None:
                existing.log(f"이벤트 수신: {env.type.value} ({env.source})")
            return "noted", existing
        if existing is not None:
            existing.log(f"같은 사건에 합침: {env.type.value} ({env.source}, {env.event_id})")
            return "merged", existing
        inc = Incident(
            context_id=Incident.new_context_id(),
            kind="scenario_a",
            title=self._title(env, component),
            component=component,
            opened_at=env.occurred_at,
        )
        inc.log(f"이벤트 수신: {env.type.value} ({env.source}, {iso(env.occurred_at)})")
        self.incidents[inc.context_id] = inc
        self._tasks[inc.context_id] = asyncio.create_task(self.engine.run(inc, self.plan_factory()))
        return "opened", inc

    @staticmethod
    def _title(env: EventEnvelope, component: str) -> str:
        metric = env.data.get("metric_code")
        suffix = f" ({metric})" if metric else ""
        return f"{component} 장애 감지{suffix}"

    async def wait(self, context_id: str) -> Incident:
        return await self._tasks[context_id]

    def hold_decision(self, context_id: str, step_id: str, approved: bool) -> bool:
        inc = self.incidents.get(context_id)
        return bool(inc and self.engine.decide_hold(inc, step_id, approved))

    def listing(self) -> list[dict[str, Any]]:
        return [i.view() for i in sorted(self.incidents.values(), key=lambda i: i.opened_at)]
