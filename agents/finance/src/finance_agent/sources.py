"""원천 데이터 조회. 재무는 요청 쪽 숫자를 믿지 않고 여기서 받은 값으로 다시 계산한다.

- 목 모드(`FINANCE_MOCK=1` 또는 `AGENT_TEMPLATE_MOCK=1`): 패키지 안 `mock_seed.json`
- 실제: data 에이전트(`impact_analysis`, `fraud_score`)를 A2A로 부른다. 판매 사이트 DB에는 붙지
  않는다. 결제 조회 API(shop-api 2차)가 생기기 전까지 결제 금액은 None이다.

못 받은 값은 None이고, 호출한 쪽은 None을 승인 근거로 쓰지 않는다(fail-closed).
TODO: data `impact_analysis` 출력에 영향 고객 ID 목록(`affected_customer_ids`)을 요청 중 (to:data).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Protocol

from ax_agent_template import call_agent
from ax_contracts.schemas import AgentRequest, RequestMeta, TaskStatus

DATA_CALL_SECONDS = 10.0
IMPACT_FIELDS = ("failed_order_count", "affected_customer_count", "damage_amount")


@dataclass(frozen=True)
class Impact:
    failed_orders: int
    affected_customers: int
    lost_amount: int
    affected_customer_ids: frozenset[str] = field(default_factory=frozenset)
    ref: str = ""


class Sources(Protocol):
    async def impact(
        self, meta: RequestMeta, context_id: str, window: dict[str, Any] | None
    ) -> Impact | None: ...

    async def paid_amount(self, meta: RequestMeta, order_id: str) -> int | None: ...

    async def fraud_score(self, meta: RequestMeta, seller_id: str) -> int | None: ...


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _impact_from(output: dict[str, Any], ref: str) -> Impact | None:
    # data `ImpactCounts` 필드 이름 (agents/data impact.py, DEP-DATA-02)
    counts = [_int(output.get(k)) for k in IMPACT_FIELDS]
    if any(c is None for c in counts):
        return None
    ids = output.get("affected_customer_ids") or []
    return Impact(
        failed_orders=counts[0],  # type: ignore[arg-type]
        affected_customers=counts[1],  # type: ignore[arg-type]
        lost_amount=counts[2],  # type: ignore[arg-type]
        affected_customer_ids=frozenset(i for i in ids if isinstance(i, str)),
        ref=ref,
    )


class MockSources:
    """시드 데이터. 단독 테스트와 CI에서 쓴다."""

    def __init__(self, seed: dict[str, Any] | None = None) -> None:
        if seed is None:
            text = resources.files("finance_agent").joinpath("mock_seed.json").read_text("utf-8")
            seed = json.loads(text)
        self.seed = seed

    async def impact(
        self, meta: RequestMeta, context_id: str, window: dict[str, Any] | None
    ) -> Impact | None:
        row = self.seed.get("impacts", {}).get(context_id)
        return _impact_from(row, f"mock:impact:{context_id}") if row else None

    async def paid_amount(self, meta: RequestMeta, order_id: str) -> int | None:
        return _int(self.seed.get("payments", {}).get(order_id))

    async def fraud_score(self, meta: RequestMeta, seller_id: str) -> int | None:
        return _int(self.seed.get("fraud_scores", {}).get(seller_id))


class LiveSources:
    """data 에이전트 A2A 호출. 응답이 없거나 형식이 다르면 None."""

    async def _ask_data(
        self, meta: RequestMeta, skill: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        deadline = min(meta.deadline, datetime.now(UTC) + timedelta(seconds=DATA_CALL_SECONDS))
        request = AgentRequest(
            meta=meta.model_copy(update={"deadline": deadline}), skill=skill, input=payload
        )
        result = await call_agent("data", request)
        return result.output if result.status == TaskStatus.COMPLETED else None

    async def impact(
        self, meta: RequestMeta, context_id: str, window: dict[str, Any] | None
    ) -> Impact | None:
        payload: dict[str, Any] = {"context_id": context_id, **(window or {})}
        output = await self._ask_data(meta, "impact_analysis", payload)
        return _impact_from(output, f"data:impact_analysis:{context_id}") if output else None

    async def paid_amount(self, meta: RequestMeta, order_id: str) -> int | None:
        return None  # TODO: shop-api 결제 조회 API (2차 요청, 재헌)

    async def fraud_score(self, meta: RequestMeta, seller_id: str) -> int | None:
        output = await self._ask_data(meta, "fraud_score", {"seller_id": seller_id})
        return _int(output.get("risk_score")) if output else None
