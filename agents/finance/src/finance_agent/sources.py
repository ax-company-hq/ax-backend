"""원천 데이터 조회. 재무는 요청 쪽 숫자를 믿지 않고 여기서 받은 값으로 다시 계산한다.

- 목 모드(`FINANCE_MOCK=1` 또는 `AGENT_TEMPLATE_MOCK=1`): 패키지 안 `mock_seed.json`
- 실제: data 에이전트(`impact_analysis`, `fraud_score`)를 A2A로 부르고, 결제 금액은 shop-api
  주문 조회(`GET /api/shop/orders/{order_id}`)의 성공 결제 합계로 본다.
  판매 사이트 DB에는 붙지 않는다.

못 받은 값은 None이고, 호출한 쪽은 None을 승인 근거로 쓰지 않는다(fail-closed).
data `impact_analysis`의 입력·출력은 계약(`ImpactAnalysisInput`·`ImpactAnalysisOutput`)으로
만들고 확인한다.
장애 구간(`window_from`·`window_to`)을 모르면 data에 묻지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from ax_agent_template import call_agent
from ax_agent_template.config import agent_url
from ax_contracts.schemas import (
    AgentRequest,
    ImpactAnalysisInput,
    ImpactAnalysisOutput,
    RequestMeta,
    TaskStatus,
)

DATA_CALL_SECONDS = 10.0
SHOP_CALL_SECONDS = 5.0


def order_key(order_id: object) -> str | None:
    """주문 번호를 문자열로 맞춘다. 고객지원은 숫자, shop-api는 `ord_` 문자열을 쓴다."""
    if isinstance(order_id, bool):
        return None
    if isinstance(order_id, int) and order_id > 0:
        return str(order_id)
    if isinstance(order_id, str) and order_id.strip():
        return order_id.strip()
    return None


def paid_total(order: dict[str, Any]) -> int | None:
    """shop-api 주문 응답에서 성공한 결제 금액 합계. 결제가 없으면 0, 형식이 다르면 None."""
    payments = order.get("payments")
    if not isinstance(payments, list):
        return None
    total = 0
    for p in payments:
        if not isinstance(p, dict):
            return None
        if p.get("status") == "succeeded":
            amount = _int(p.get("amount"))
            if amount is None:
                return None
            total += amount
    return total


IMPACT_FIELDS = ("failed_order_count", "affected_customer_count", "damage_amount")


@dataclass(frozen=True)
class Impact:
    failed_orders: int
    affected_customers: int
    lost_amount: int
    affected_customer_ids: frozenset[str] = field(default_factory=frozenset)
    ref: str = ""
    by_category: tuple[tuple[str, int, int], ...] = ()  # (category, 실패 주문 수, 피해 금액)


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
    by_category = []
    for row in output.get("by_category") or []:
        if not isinstance(row, dict) or not isinstance(row.get("category"), str):
            continue
        orders, amount = _int(row.get("failed_order_count")), _int(row.get("damage_amount"))
        if orders is not None and amount is not None:
            by_category.append((row["category"], orders, amount))
    return Impact(
        failed_orders=counts[0],  # type: ignore[arg-type]
        affected_customers=counts[1],  # type: ignore[arg-type]
        lost_amount=counts[2],  # type: ignore[arg-type]
        affected_customer_ids=frozenset(i for i in ids if isinstance(i, str)),
        ref=ref,
        by_category=tuple(by_category),
    )


def impact_input(window: dict[str, Any] | None) -> ImpactAnalysisInput | None:
    """재무가 아는 사건 구간 → data `impact_analysis` 입력. 구간이 없거나 틀리면 None."""
    if not window:
        return None
    try:
        return ImpactAnalysisInput.model_validate(
            {k: window.get(k) for k in ("window_from", "window_to", "category") if window.get(k)}
        )
    except ValidationError:
        return None


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
        return _int(self.seed.get("payments", {}).get(str(order_id)))

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
        """사건 구간의 영향. 구간을 모르면 묻지 않고 None (data 계약은 구간이 필수다).

        사건 번호는 입력이 아니라 요청 meta(`context_id`)로 전달된다.
        """
        payload = impact_input(window)
        if payload is None:
            return None
        output = await self._ask_data(
            meta, "impact_analysis", payload.model_dump(mode="json", exclude_none=True)
        )
        if output is None:
            return None
        try:
            ImpactAnalysisOutput.model_validate(output)
        except ValidationError:
            return None
        return _impact_from(output, f"data:impact_analysis:{context_id}")

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client  # 테스트용

    async def paid_amount(self, meta: RequestMeta, order_id: str) -> int | None:
        """shop-api 주문 조회. 주문이 없거나(404) 응답이 없으면 None(승인 근거로 쓰지 않음)."""
        url = f"{agent_url('shop-api')}/api/shop/orders/{order_id}"
        client = self._http or httpx.AsyncClient()
        try:
            response = await client.get(url, timeout=SHOP_CALL_SECONDS)
        except httpx.HTTPError:
            return None
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        order = body.get("data") if isinstance(body, dict) else None
        return paid_total(order) if isinstance(order, dict) else None

    async def fraud_score(self, meta: RequestMeta, seller_id: str) -> int | None:
        output = await self._ask_data(meta, "fraud_score", {"seller_id": seller_id})
        return _int(output.get("risk_score")) if output else None
