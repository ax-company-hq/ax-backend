"""예시: shop-api의 실패 주문 목록 조회 (data 에이전트가 사용).

각 서비스 담당이 이 파일을 복사해 자기 조회 형식을 같은 방식으로 추가한다.
`buyer_id`는 가명 ID만 쓴다. 금액은 정수 원이다.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class ShopFailedOrdersQuery(AxModel):
    """입력. `from`은 파이썬 예약어라 별칭으로 받는다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime
    category: str | None = None
    tenant_id: str = TENANT_ID_DEFAULT


class FailedOrder(AxModel):
    order_id: str
    buyer_id: str
    amount: int
    category: str
    failed_at: UtcDatetime
    reason: str
    tenant_id: str = TENANT_ID_DEFAULT


class ShopFailedOrdersResult(AxModel):
    """출력. 조회 자체의 근거(쿼리·시각)는 MCP 봉투와 `Evidence`로 붙인다."""

    orders: list[FailedOrder] = Field(default_factory=list)
