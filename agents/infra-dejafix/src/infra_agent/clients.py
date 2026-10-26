"""바깥 호출: 판매 사이트 장애 상태·해제(shop-api), 업무 지표 이상 탐지(data).

목 모드에서 주소(`AX_SHOP_API_URL`·`AX_DATA_URL`)가 없으면 가짜 응답을 쓴다(CI 단독 테스트용).
주소가 있으면 목 모드여도 실제로 부른다(템플릿 `execute()`와 같은 관례).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from ax_agent_template import call_agent
from ax_agent_template.config import agent_token, is_mock
from ax_contracts.schemas import AgentRequest, RequestMeta, TaskStatus

FAULTS_PATH = "/api/shop/admin/faults"
SHOP_TIMEOUT_SECONDS = 10.0
DATA_TIMEOUT_SECONDS = 20.0
PAYMENT_METRICS = ["payment_fail_rate", "order_fail_count"]


class ShopClient(Protocol):
    async def fault_state(self) -> dict[str, Any]: ...

    async def release_fault(self) -> dict[str, Any]: ...


class DataClient(Protocol):
    async def payment_metrics_ok(
        self, meta: RequestMeta, since: datetime, now: datetime
    ) -> tuple[bool | None, str]: ...


@dataclass
class MockShop:
    """목 판매 사이트. 기본은 장애 없음. 테스트에서 상태를 바꿔 쓴다."""

    state: dict[str, Any] = field(
        default_factory=lambda: {"active": False, "failing": False, "degraded": False}
    )
    released: int = 0

    async def fault_state(self) -> dict[str, Any]:
        return dict(self.state)

    async def release_fault(self) -> dict[str, Any]:
        self.released += 1
        self.state.update(active=False, failing=False, degraded=False)
        return dict(self.state)


@dataclass
class MockData:
    """목 데이터분석. 기본은 업무 지표 정상."""

    ok: bool | None = True
    calls: int = 0

    async def payment_metrics_ok(
        self, meta: RequestMeta, since: datetime, now: datetime
    ) -> tuple[bool | None, str]:
        self.calls += 1
        return self.ok, "목 데이터분석"


class HttpShop:
    """shop-api 관리 API. 해제는 infra 토큰만 허용된다(shop-api RELEASE_ALLOWED)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {agent_token('infra')}"}

    async def fault_state(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=SHOP_TIMEOUT_SECONDS) as client:
            response = await client.get(self.base_url + FAULTS_PATH, headers=self._headers())
        response.raise_for_status()
        return dict(response.json().get("data") or {})

    async def release_fault(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=SHOP_TIMEOUT_SECONDS) as client:
            response = await client.delete(self.base_url + FAULTS_PATH, headers=self._headers())
        response.raise_for_status()
        return dict(response.json().get("data") or {})


class A2AData:
    """data `anomaly_detection`을 A2A로 부른다 (같은 사건 context_id)."""

    async def payment_metrics_ok(
        self, meta: RequestMeta, since: datetime, now: datetime
    ) -> tuple[bool | None, str]:
        deadline = min(meta.deadline, datetime.now(UTC) + timedelta(seconds=DATA_TIMEOUT_SECONDS))
        request = AgentRequest(
            meta=meta.model_copy(update={"deadline": deadline, "idempotency_key": None}),
            skill="anomaly_detection",
            input={
                "window_from": since.isoformat(),
                "window_to": now.isoformat(),
                "metrics": PAYMENT_METRICS,
            },
        )
        result = await call_agent("data", request)
        if result.status != TaskStatus.COMPLETED:
            return None, f"data anomaly_detection {result.status.value} ({result.outcome})"
        any_anomaly = result.output.get("any_anomaly")
        if not isinstance(any_anomaly, bool):
            return None, "data anomaly_detection 응답에 any_anomaly가 없음"
        return (not any_anomaly), str(result.output.get("summary") or "")


def default_shop() -> ShopClient:
    url = os.getenv("AX_SHOP_API_URL")
    if url:
        return HttpShop(url)
    if is_mock("infra"):
        return MockShop()
    return HttpShop("http://shop-api:8010")


def default_data() -> DataClient:
    if is_mock("infra") and not os.getenv("AX_DATA_URL"):
        return MockData()
    return A2AData()
