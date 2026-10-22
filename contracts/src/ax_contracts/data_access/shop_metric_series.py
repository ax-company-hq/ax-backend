"""shop-api의 지표 시계열 조회 (data 에이전트 anomaly_detection이 사용).

조회 근거(collected_at, truncated)는 MCP 봉투로 붙인다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime

MetricName = Literal["order_count", "order_fail_count", "payment_fail_rate", "payment_count"]


class ShopMetricSeriesQuery(AxModel):
    """입력. `from`은 파이썬 예약어라 별칭으로 받는다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    metric: MetricName
    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime
    bucket: Literal["5m"] = "5m"
    tenant_id: str = TENANT_ID_DEFAULT


class MetricPoint(AxModel):
    ts: UtcDatetime  # 버킷 시작 시각
    value: float


class ShopMetricSeriesResult(AxModel):
    metric: MetricName
    bucket: Literal["5m"] = "5m"
    points: list[MetricPoint] = Field(default_factory=list)
