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
from ax_safety.rules import load_registry, load_thresholds
from ax_safety.store import Review, Store

START = datetime(2026, 10, 22, 5, 40, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("SAFETY_HUMAN_TOKENS", raising=False)
    monkeypatch.delenv("SAFETY_MOCK", raising=False)  # CI는 SAFETY_MOCK=1이라 테스트마다 정한다
    monkeypatch.delenv("SAFETY_REVIEW_TIMEOUT_SECONDS", raising=False)  # 기본은 기준표 값(10분)
    monkeypatch.delenv("SAFETY_AUTO_REVIEW", raising=False)  # AI 1차 판단은 기본 꺼짐
    # 바뀐 기준 저장 파일은 테스트마다 임시 폴더에 (레포에 쓰지 않게)
    monkeypatch.setenv(
        "SAFETY_THRESHOLD_OVERRIDES_PATH", str(tmp_path / "threshold_overrides.json")
    )
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
    # 기본 테스트는 사람 확인 흐름을 본다. AI 1차 판단은 test_auto_review.py에서 켠다
    table = load_thresholds()
    table = table.model_copy(
        update={"auto_review": table.auto_review.model_copy(update={"enabled": False})}
    )
    return Gate(load_registry(), table, Store(), verifier, sent, now=clock)


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
