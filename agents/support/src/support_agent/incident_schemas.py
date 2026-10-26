"""사건(장애)으로 피해를 본 고객 보상 skill의 입출력."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel

MAX_BUYERS = 50
BUYER_PATTERN = r"^buy_\d{4}$"


class RequestIncidentCompensationInput(AxModel):
    """영향 고객 목록은 데이터분석 영향 분석 결과를 총괄이 넘긴다 (문의를 안 낸 고객도 있다).

    단가와 건수 상한은 정책표(`compensation.py`)가 정한다. 사유는 결제 장애만 받는다.
    """

    buyer_ids: list[str] = Field(min_length=1, max_length=MAX_BUYERS)
    reason_code: Literal["payment_failure"] = "payment_failure"
    count: int = Field(default=1, ge=1, le=3)
    impact_ref: str | None = Field(default=None, max_length=100)  # 영향 분석 결과 참조 (근거용)


class IncidentCouponResult(AxModel):
    buyer_id: str
    status: Literal["requested", "held", "duplicate", "failed", "skipped"]
    sequence: int | None = None
    marketing_status: str | None = None
    marketing_outcome: str | None = None
    coupon_id: str | None = None
    total_value: int = 0


class IncidentCompensationOutput(AxModel):
    reason_code: str
    unit_value: int
    count: int
    requested: int
    results: list[IncidentCouponResult]
    stopped_early: bool = False  # 마케팅에 닿지 않아 남은 고객은 보내지 않았다
    notes: list[str] = Field(default_factory=list)
