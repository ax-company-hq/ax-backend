"""shop-api의 판매자 정보 조회 (data 에이전트 fraud_score가 사용).

가명 ID만 쓴다. 사업자 정보는 원문 대신 sha256 해시로만 받는다.
"""

from __future__ import annotations

from pydantic import Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class ShopSellerProfileQuery(AxModel):
    seller_id: str
    tenant_id: str = TENANT_ID_DEFAULT


class ShopSellerProfile(AxModel):
    seller_id: str
    joined_at: UtcDatetime
    business_hash: str  # 사업자 정보 sha256
    business_hash_duplicated: bool  # 같은 해시의 다른 판매자 존재
    product_count: int = Field(ge=0)
    listings_last_24h: int = Field(ge=0)
    tenant_id: str = TENANT_ID_DEFAULT
