import pytest
from pydantic import ValidationError

from ax_contracts.data_access.orchestrator_incidents import (
    OrchestratorIncident,
    OrchestratorIncidentsQuery,
    OrchestratorIncidentsResult,
)
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


# ------------------------------------------------- 총괄 사건 목록 (#417 제안 A, #418)
INCIDENT = {
    "context_id": "ctx_inc_0001",
    "component": "payment",
    "status": "resolved",
    "opened_at": "2026-10-22T05:00:00Z",
    "closed_at": "2026-10-22T05:30:00Z",
}


def test_orchestrator_incidents_query_defaults_and_alias() -> None:
    query = OrchestratorIncidentsQuery.model_validate({"from": "2026-10-22T05:00:00Z"})
    assert (query.limit, query.tenant_id) == (1000, "shop-demo")
    assert (query.to, query.statuses) == (None, None)
    assert query.from_.utcoffset().total_seconds() == 0
    assert OrchestratorIncidentsQuery.model_validate({}).from_ is None  # 조건 없이 전부
    with pytest.raises(ValidationError):
        OrchestratorIncidentsQuery.model_validate({"limit": 0})
    with pytest.raises(ValidationError):
        OrchestratorIncidentsQuery.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):  # 시간대 없는 시각
        OrchestratorIncidentsQuery.model_validate({"from": "2026-10-22T05:00:00"})


def test_orchestrator_incident_keeps_only_the_agreed_fields() -> None:
    row = OrchestratorIncident.model_validate(INCIDENT)
    assert (row.context_id, row.component, row.status) == ("ctx_inc_0001", "payment", "resolved")
    assert row.closed_at.utcoffset().total_seconds() == 0
    assert row.tenant_id == "shop-demo"
    # 아직 열린 사건: 닫힌 시각·부품을 모를 수 있다
    open_row = OrchestratorIncident.model_validate(
        {"context_id": "ctx_inc_0002", "status": "open", "opened_at": "2026-10-22T05:00:00Z"}
    )
    assert (open_row.closed_at, open_row.component) == (None, None)
    # 합의 밖의 키는 거부한다 (읽는 쪽이 다섯 키만 골라내고 검증하게 만든다)
    for extra in ("steps", "events", "summary", "metrics", "pending_holds"):
        with pytest.raises(ValidationError):
            OrchestratorIncident.model_validate({**INCIDENT, extra: []})
    with pytest.raises(ValidationError):  # 시간대 없는 시각
        OrchestratorIncident.model_validate({**INCIDENT, "opened_at": "2026-10-22T05:00:00"})


def test_orchestrator_incidents_result() -> None:
    empty = OrchestratorIncidentsResult.model_validate({})
    assert (empty.incidents, empty.collected_at, empty.truncated) == ([], None, False)
    result = OrchestratorIncidentsResult.model_validate(
        {"incidents": [INCIDENT], "collected_at": "2026-10-27T06:00:00Z", "truncated": True}
    )
    assert result.incidents[0].context_id == "ctx_inc_0001" and result.truncated is True
    with pytest.raises(ValidationError):
        OrchestratorIncidentsResult.model_validate({"incidents": [INCIDENT], "unknown": 1})
