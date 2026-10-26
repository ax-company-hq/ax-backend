"""결정적 시드 — 결제 장애 시나리오의 실패 주문 (목 shop 클라이언트용).

장애 구간은 2026-10-21T05:00Z ~ 05:30Z다. 구간 안 실패 주문 6건(같은 buyer 여러 주문 포함,
books·stationery 섞임)과 구간 밖 3건을 둔다. 아래 정답 상수는 시드에서 손으로 센 값이며,
테스트가 `aggregate_impact` 결과와 맞는지 확인한다.

구매자 ID는 shop-api 시드와 같은 가명 형식 `buy_<4자리>`다 (고객지원 보상 요청이 이 형식만 받는다).
목 모드에서 요청 구간이 `DEMO_ALIGN_FROM` 이후면 `MockShopClient`가 시드를 요청 구간으로 옮긴다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ax_contracts.data_access.shop_failed_orders import FailedOrder

INCIDENT_FROM = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)
INCIDENT_TO = datetime(2026, 10, 21, 5, 30, tzinfo=UTC)

_REASON = "payment_gateway_timeout"


def _o(order_id: str, buyer_id: str, amount: int, category: str, minute: int, hour: int = 5):
    return FailedOrder(
        order_id=order_id,
        buyer_id=buyer_id,
        amount=amount,
        category=category,
        failed_at=datetime(2026, 10, 21, hour, minute, tzinfo=UTC),
        reason=_REASON,
    )


SEED_FAILED_ORDERS: list[FailedOrder] = [
    # 장애 구간 안 (6건, 고객 4명)
    _o("ord_seed001", "buy_0001", 12000, "books", 3),
    _o("ord_seed002", "buy_0001", 8000, "stationery", 10),
    _o("ord_seed003", "buy_0002", 25000, "books", 12),
    _o("ord_seed004", "buy_0003", 4500, "stationery", 20),
    _o("ord_seed005", "buy_0003", 15500, "books", 25),
    _o("ord_seed006", "buy_0004", 30000, "books", 29),
    # 장애 구간 밖 (3건)
    _o("ord_seed007", "buy_0001", 7000, "books", 55, hour=4),
    _o("ord_seed008", "buy_0005", 9000, "stationery", 45),
    _o("ord_seed009", "buy_0002", 3000, "books", 30, hour=6),
]

# 정답 상수 (구간 안, 전체 카테고리)
EXPECTED_FAILED_ORDER_COUNT = 6
EXPECTED_AFFECTED_CUSTOMER_COUNT = 4
EXPECTED_DAMAGE_AMOUNT = 95000  # 12000+8000+25000+4500+15500+30000

# 정답 상수 (구간 안, books만)
EXPECTED_BOOKS_ORDER_COUNT = 4
EXPECTED_BOOKS_CUSTOMER_COUNT = 4  # buy_0001 ~ buy_0004
EXPECTED_BOOKS_DAMAGE_AMOUNT = 82500  # 12000+25000+15500+30000

# 데모 정렬 시작 시각. 이 시각 이후에 시작하는 구간을 물으면 목 shop·지표가 시드 장애를
# 그 구간으로 옮긴다. 그 전(시드 날짜 ~ 지표 이력 끝 다음 날)은 고정 날짜 그대로다.
DEMO_ALIGN_FROM = datetime(2026, 10, 23, 0, 0, tzinfo=UTC)
