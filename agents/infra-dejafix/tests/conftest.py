"""infra 테스트 공용 설정: 목 모드 고정, 테스트마다 새 런타임(가짜 시계·대기·판매 사이트·데이터)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ax_agent_template import config
from ax_agent_template.testing import reset_state  # noqa: F401  autouse, 템플릿 상태 초기화
from ax_contracts.schemas import ExecuteDecision
from infra_agent import handlers
from infra_agent.clients import MockData, MockShop
from infra_agent.deployments import DeploymentHistory
from infra_agent.runtime import Runtime, set_runtime

NOW = datetime(2026, 10, 21, 5, 10, tzinfo=UTC)  # v2.4.1 배포(05:02Z) 8분 뒤


class FakeSleep:
    def __init__(self, clock: dict[str, datetime]) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock["now"] += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def runtime(monkeypatch: pytest.MonkeyPatch, reset_state: None) -> Runtime:  # noqa: F811
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("AX_SAFETY_URL", raising=False)
    monkeypatch.delenv("AX_SHOP_API_URL", raising=False)
    monkeypatch.delenv("AX_DATA_URL", raising=False)
    config.set_agent_name("infra")
    clock = {"now": NOW}
    return set_runtime(
        Runtime(
            history=DeploymentHistory.load(),
            shop=MockShop(state={"active": True, "failing": True, "degraded": True}),
            data=MockData(),
            clock=lambda: clock["now"],
            sleep=FakeSleep(clock),
        )
    )


def _a2a_body(
    skill: str,
    payload: dict[str, Any],
    *,
    token: str = "mock:orchestrator",
    path: list[str] | None = None,
    context_id: str = "ctx_inc_0001",
    deadline_s: int = 300,
    untrusted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "meta": {
            "context_id": context_id,
            "token": token,
            "path": path if path is not None else ["orchestrator"],
            "deadline": (datetime.now(UTC) + timedelta(seconds=deadline_s)).isoformat(),
            "idempotency_key": f"{context_id}:{skill}:step",
        },
        "skill": skill,
        "input": payload,
        "untrusted": untrusted or {},
    }


@pytest.fixture
def a2a_body():
    """A2A 요청 본문을 만드는 함수 (기본 요청자 orchestrator, 사건 ctx_inc_0001)."""
    return _a2a_body


class FakeGate:
    """`execute()` 대역. 받은 요청을 모으고 정해 둔 판정을 차례로 돌려준다."""

    def __init__(self, *decisions: ExecuteDecision) -> None:
        self.decisions = list(decisions)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, action, params, context, evidence) -> ExecuteDecision:
        self.calls.append({"action": action, "params": params, "key": context.idempotency_key})
        return self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]


def executed(grade: str = "B") -> ExecuteDecision:
    return ExecuteDecision(execution_id="exe_0001", grade=grade, status="executed")


def held() -> ExecuteDecision:
    return ExecuteDecision(
        execution_id="exe_0002",
        grade="C",
        status="held",
        hold_reason="over_threshold",
        review_id="rev_0001",
    )


def _spec(spec: ExecuteDecision | str) -> ExecuteDecision:
    if isinstance(spec, ExecuteDecision):
        return spec
    kind, _, grade = spec.partition(":")
    return held() if kind == "held" else executed(grade or "B")


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    def install(*specs: ExecuteDecision | str) -> FakeGate:
        """판정 목록. 문자열은 `"executed"`·`"executed:C"`·`"held"`로 줄여 쓴다."""
        decisions = [_spec(s) for s in specs]
        fake = FakeGate(*decisions)
        monkeypatch.setattr(handlers, "execute", fake)
        return fake

    return install
