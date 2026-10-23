import pytest
from pydantic import ValidationError

from ax_contracts.data_access.shop_failed_orders import ShopFailedOrdersQuery
from ax_contracts.data_access.shop_metric_series import (
    ShopMetricSeriesQuery,
    ShopMetricSeriesResult,
)
from ax_contracts.data_access.shop_seller_prices import (
    SellerProductPrice,
    ShopSellerPricesResult,
)
from ax_contracts.data_access.shop_seller_profile import ShopSellerProfile

PROFILE = {
    "seller_id": "sel_1",
    "joined_at": "2026-10-01T00:00:00Z",
    "business_hash": "a" * 64,
    "business_hash_duplicated": False,
    "product_count": 3,
    "listings_last_24h": 1,
}
PRICE = {"product_id": "prd_1", "category": "book", "price": 12000, "category_median_price": 15000}


def test_metric_series_query_accepts_from_alias() -> None:
    query = ShopMetricSeriesQuery.model_validate(
        {
            "metric": "payment_fail_rate",
            "from": "2026-10-22T05:00:00Z",
            "to": "2026-10-22T06:00:00Z",
        }
    )
    assert query.bucket == "5m"
    assert query.tenant_id == "shop-demo"
    assert query.from_.utcoffset().total_seconds() == 0


def test_metric_series_rejects_bad_input() -> None:
    base = {"metric": "order_count", "from": "2026-10-22T05:00:00Z", "to": "2026-10-22T06:00:00Z"}
    with pytest.raises(ValidationError):
        ShopMetricSeriesQuery.model_validate({**base, "unknown": 1})
    with pytest.raises(ValidationError):
        ShopMetricSeriesQuery.model_validate({**base, "metric": "cpu"})
    with pytest.raises(ValidationError):
        ShopMetricSeriesQuery.model_validate({**base, "bucket": "1h"})
    with pytest.raises(ValidationError):  # 시간대 없는 시각
        ShopMetricSeriesQuery.model_validate({**base, "from": "2026-10-22T05:00:00"})


def test_metric_series_result() -> None:
    result = ShopMetricSeriesResult.model_validate(
        {
            "metric": "order_count",
            "bucket": "5m",
            "points": [{"ts": "2026-10-22T05:00:00Z", "value": 12}],
        }
    )
    assert result.points[0].value == 12.0
    with pytest.raises(ValidationError):
        ShopMetricSeriesResult.model_validate(
            {"metric": "order_count", "points": [{"ts": "2026-10-22T05:00:00", "value": 1}]}
        )


def test_seller_profile() -> None:
    assert ShopSellerProfile.model_validate(PROFILE).tenant_id == "shop-demo"
    with pytest.raises(ValidationError):
        ShopSellerProfile.model_validate({**PROFILE, "business_number": "123-45-67890"})
    with pytest.raises(ValidationError):
        ShopSellerProfile.model_validate({**PROFILE, "joined_at": "2026-10-01T00:00:00"})


def test_seller_prices() -> None:
    result = ShopSellerPricesResult.model_validate({"items": [PRICE]})
    assert result.items[0].price == 12000
    with pytest.raises(ValidationError):
        SellerProductPrice.model_validate({**PRICE, "price": -1})
    with pytest.raises(ValidationError):
        SellerProductPrice.model_validate({**PRICE, "category_median_price": -1})
    with pytest.raises(ValidationError):
        SellerProductPrice.model_validate({**PRICE, "price": 100.5})
    with pytest.raises(ValidationError):
        SellerProductPrice.model_validate({**PRICE, "unknown": 1})


def test_metric_series_result_evidence_fields_are_optional() -> None:
    plain = ShopMetricSeriesResult.model_validate({"metric": "order_count"})
    assert (plain.collected_at, plain.truncated, plain.tenant_id) == (None, False, "shop-demo")
    stamped = ShopMetricSeriesResult.model_validate(
        {"metric": "order_count", "collected_at": "2026-10-22T06:00:00Z", "truncated": True}
    )
    assert stamped.truncated is True and stamped.collected_at is not None
    with pytest.raises(ValidationError):  # 시간대 없는 조회 시각
        ShopMetricSeriesResult.model_validate(
            {"metric": "order_count", "collected_at": "2026-10-22T06:00:00"}
        )


def test_failed_orders_limit_range() -> None:
    base = {"from": "2026-10-22T05:00:00Z", "to": "2026-10-22T06:00:00Z"}
    assert ShopFailedOrdersQuery.model_validate(base).limit == 1000
    assert ShopFailedOrdersQuery.model_validate({**base, "limit": 50}).limit == 50
    with pytest.raises(ValidationError):
        ShopFailedOrdersQuery.model_validate({**base, "limit": 0})
