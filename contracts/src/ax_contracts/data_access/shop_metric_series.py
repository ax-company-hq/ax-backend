"""shop-api의 지표 시계열 조회 (data 에이전트 anomaly_detection이 사용).

지표는 shop-api가 DB에서 코드로 계산한다 (LLM 없음).
- `order_count`: 버킷 안에 접수된 주문 수
- `order_fail_count`: 버킷 안에 결제가 실패한 주문 수 (`payment.failed` 기준)
- `payment_fail_rate`: 버킷 안 결제 시도 중 실패 비율 `실패 / (성공 + 실패)`
- `payment_count`: 버킷 안 결제 시도 수

조회 시각(`collected_at`)과 잘림 여부(`truncated`)는 MCP 공통 봉투와 같은 형태이며, 기존 소비자와
호환되도록 선택 필드다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime

MetricName = Literal["order_count", "order_fail_count", "payment_fail_rate", "payment_count"]
# 버킷은 5m만 합의됨(#67). 1m·15m·1h는 data 확인 뒤 추가한다(#86)
Bucket = Literal["5m"]


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
    value: float


class ShopMetricSeriesResult(AxModel):
    metric: MetricName
    bucket: Bucket = "5m"
    points: list[MetricPoint] = Field(default_factory=list)
    collected_at: UtcDatetime | None = None
    truncated: bool = False
    tenant_id: str = TENANT_ID_DEFAULT
