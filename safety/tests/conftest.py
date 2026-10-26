"""safety 테스트 공용: 목 모드, 가짜 검증자, 고정 시계, 요청 만들기."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_contracts.schemas import ExecuteRequest
from ax_safety.gate import Gate, VerifyOutcome, VerifyTask
from ax_safety.main import create_app
from ax_safety.rules import (
    ActionSpec,
    AmountSpec,
    Registry,
    Rule,
    load_registry,
    load_thresholds,
)
from ax_safety.store import Review, Store

START = datetime(2026, 10, 22, 5, 40, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("SAFETY_HUMAN_TOKENS", raising=False)
    monkeypatch.delenv("SAFETY_MOCK", raising=False)  # CI는 SAFETY_MOCK=1이라 테스트마다 정한다
    monkeypatch.delenv("SAFETY_REVIEW_TIMEOUT_SECONDS", raising=False)  # 기본은 기준표 값(10분)
    events.reset_mock_bus()


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeVerifier:
    """검증자 대역. `result`를 바꿔 승인·거절·무응답을 흉내 낸다. 받은 작업을 모아 둔다."""

    def __init__(self, result: str = "approved") -> None:
        self.result = result
        self.tasks: list[VerifyTask] = []

    async def __call__(self, task: VerifyTask) -> VerifyOutcome:
        self.tasks.append(task)
        if self.result == "raise":
            raise ConnectionError("검증자에 닿지 않음")
        return VerifyOutcome(self.result)  # type: ignore[arg-type]


class Events:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event_type: str, source: ExecuteRequest | Review, data: dict) -> None:
        self.sent.append((event_type, data))

    def types(self) -> list[str]:
        return [t for t, _ in self.sent]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier() -> FakeVerifier:
    return FakeVerifier()


@pytest.fixture
def sent() -> Events:
    return Events()


@pytest.fixture
def gate(clock: Clock, verifier: FakeVerifier, sent: Events) -> Gate:
    return Gate(load_registry(), load_thresholds(), Store(), verifier, sent, now=clock)


@pytest.fixture
def client(gate: Gate) -> TestClient:
    return TestClient(create_app(gate, expire_interval=None))


def _coupon(
    count: int = 4,
    unit_value: int = 5000,
    *,
    key: str | None = None,
    buyer_id: str = "buy_a1",
    purpose_code: str = "compensation",
    context_id: str = "ctx_inc_0001",
    claimed_grade: str | None = None,
    **extra: Any,
) -> ExecuteRequest:
    params = {
        "purpose_code": purpose_code,
        "buyer_id": buyer_id,
        "count": count,
        "unit_value": unit_value,
        "total_value": count * unit_value,
        **extra,
    }
    return ExecuteRequest(
        action="issue_coupon",
        requester="marketing",
        context_id=context_id,
        idempotency_key=key or f"{context_id}:issue_coupon:{buyer_id}:{count}x{unit_value}",
        params=params,
        claimed_grade=claimed_grade,
    )


def _request(action: str, requester: str, key: str, **params: Any) -> ExecuteRequest:
    return ExecuteRequest(
        action=action,
        requester=requester,
        context_id="ctx_inc_0001",
        idempotency_key=key,
        params=params,
    )


@pytest.fixture
def coupon():
    """쿠폰 실행 요청을 만든다: `coupon(count, unit_value, key=..., purpose_code=...)`."""
    return _coupon


@pytest.fixture
def act():
    """임의 액션 요청을 만든다: `act(action, requester, key, **params)`."""
    return _request


def _seller_review(key: str) -> ExecuteRequest:
    """data가 교차검증하는 B 액션 (위험 점수 70 미만)."""
    return _request("seller_review", "finance", key, seller_id="sel_1", risk_score=20)


@pytest.fixture
def seller_review():
    """검증자가 있는 B 액션 요청을 만든다: `seller_review(key)`.

    쿠폰·환불이 A가 되어(재무 재설계) 검증자 동작은 이 액션으로 확인한다.
    """
    return _seller_review


# 쿠폰·환불이 A가 되어 실제 레지스트리에는 금액 있는 B 에이전트 검증 액션이 없다.
# 금액과 검증이 함께 걸리는 동작은 테스트 전용 `pay_bonus`(B, finance 검증)로 확인한다.
BONUS_SPEC = ActionSpec(
    action="pay_bonus",
    owner="hr",
    base_grade="B",
    verifier="finance",
    verify_skill="verify_bonus",
    amount=AmountSpec(count_field="count", unit_field="unit_value", claimed_fields=["total"]),
)
BONUS_DAILY = Rule(
    key="bonus_daily", action="pay_bonus", metric="daily_total", op=">=", value=1_000_000
)


@pytest.fixture
def bonus_gate(clock: Clock, verifier: FakeVerifier, sent: Events) -> Gate:
    """테스트 전용 `pay_bonus`(B, 일일 누적 100만 원 이상 C)를 더한 관문."""
    registry = Registry(actions=[*load_registry().actions, BONUS_SPEC])
    table = load_thresholds()
    table = table.model_copy(update={"rules": [*table.rules, BONUS_DAILY]})
    return Gate(registry, table, Store(), verifier, sent, now=clock)


def _bonus(
    count: int = 3,
    unit_value: int = 5000,
    *,
    key: str = "bonus-1",
    claimed_grade: str | None = None,
) -> ExecuteRequest:
    return ExecuteRequest(
        action="pay_bonus",
        requester="hr",
        context_id="ctx_inc_0001",
        idempotency_key=key,
        params={"count": count, "unit_value": unit_value, "total": count * unit_value},
        claimed_grade=claimed_grade,
    )


@pytest.fixture
def bonus():
    """`pay_bonus` 요청을 만든다: `bonus(count, unit_value, key=..., claimed_grade=...)`."""
    return _bonus
