"""결정적 시드 — 결제 장애 시나리오의 실패 주문 (목 shop 클라이언트용).

장애 구간은 2026-10-21T05:00Z ~ 05:30Z다. 구간 안 실패 주문 6건(같은 buyer 여러 주문 포함,
books·stationery 섞임)과 구간 밖 3건을 둔다. 아래 정답 상수는 시드에서 손으로 센 값이며,
테스트가 `aggregate_impact` 결과와 맞는지 확인한다.
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
    _o("ord_seed001", "buyer_a", 12000, "books", 3),
    _o("ord_seed002", "buyer_a", 8000, "stationery", 10),
    _o("ord_seed003", "buyer_b", 25000, "books", 12),
    _o("ord_seed004", "buyer_c", 4500, "stationery", 20),
    _o("ord_seed005", "buyer_c", 15500, "books", 25),
    _o("ord_seed006", "buyer_d", 30000, "books", 29),
    # 장애 구간 밖 (3건)
    _o("ord_seed007", "buyer_a", 7000, "books", 55, hour=4),
    _o("ord_seed008", "buyer_e", 9000, "stationery", 45),
    _o("ord_seed009", "buyer_b", 3000, "books", 30, hour=6),
]

# 정답 상수 (구간 안, 전체 카테고리)
EXPECTED_FAILED_ORDER_COUNT = 6
EXPECTED_AFFECTED_CUSTOMER_COUNT = 4
EXPECTED_DAMAGE_AMOUNT = 95000  # 12000+8000+25000+4500+15500+30000

# 정답 상수 (구간 안, books만)
EXPECTED_BOOKS_ORDER_COUNT = 4
EXPECTED_BOOKS_CUSTOMER_COUNT = 4  # buyer_a, b, c, d
EXPECTED_BOOKS_DAMAGE_AMOUNT = 82500  # 12000+25000+15500+30000
