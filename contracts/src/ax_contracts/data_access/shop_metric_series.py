"""shop-api의 지표 시계열 조회 (data 에이전트가 사용).

지표는 shop-api가 DB에서 코드로 계산한다 (LLM 없음).
- `order_count`: 버킷 안에 접수된 주문 수
- `order_fail_count`: 버킷 안에 결제가 실패한 주문 수 (`payment.failed` 기준)
- `payment_fail_rate`: 버킷 안 결제 시도 중 실패 비율 `실패 / (성공 + 실패)`. 시도가 없으면 `None`.

표본이 없는 버킷의 `value`는 `None`(확인할 수 없음)이다. 0으로 채우지 않는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime

MetricName = Literal["order_count", "payment_fail_rate", "order_fail_count"]
Bucket = Literal["1m", "5m", "15m", "1h"]


class ShopMetricSeriesQuery(AxModel):
    """입력. `from`은 파이썬 예약어라 별칭으로 받는다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    metric: MetricName
    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime
    bucket: Bucket = "5m"
    tenant_id: str = TENANT_ID_DEFAULT


class MetricPoint(AxModel):
    ts: UtcDatetime  # 버킷 시작 시각 (UTC)
    value: float | None = None  # 표본이 없으면 None (0으로 채우지 않는다)


class ShopMetricSeriesResult(AxModel):
    """출력. 조회 시각과 잘림 여부는 MCP 공통 봉투와 같은 형태다."""

    metric: MetricName
    bucket: Bucket
    points: list[MetricPoint] = Field(default_factory=list)
    collected_at: UtcDatetime
    truncated: bool = False
    tenant_id: str = TENANT_ID_DEFAULT
