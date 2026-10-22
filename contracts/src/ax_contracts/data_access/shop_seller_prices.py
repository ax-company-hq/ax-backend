"""shop-api의 판매자 상품 가격 조회 (data 에이전트 fraud_score가 사용).

금액은 정수 원(0 이상)이다.
"""

from __future__ import annotations

from pydantic import Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel


class ShopSellerPricesQuery(AxModel):
    seller_id: str
    tenant_id: str = TENANT_ID_DEFAULT


class SellerProductPrice(AxModel):
    product_id: str
    category: str
    price: int = Field(ge=0)
    category_median_price: int = Field(ge=0)


class ShopSellerPricesResult(AxModel):
    items: list[SellerProductPrice] = Field(default_factory=list)
