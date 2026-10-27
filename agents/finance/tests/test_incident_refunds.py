"""사건 장부의 환불 금액: 구간·영향 고객으로 귀속, 없으면 null. 네트워크 없음."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.incidents import incident_refunds

WINDOW = {"from": "2026-10-22T05:00:00Z", "to": "2026-10-22T05:30:00Z"}
IMPACT = frozenset({"buy_0001", "buy_0002"})


def _books(rows: list[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(refunds=rows)


def _refund(buyer: str, amount: int, hhmm: str) -> dict[str, Any]:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return {
        "refund_id": f"rfd_{buyer}_{hhmm}",
        "order_id": "ord_1",
        "buyer_id": buyer,
        "amount": amount,
        "created_at": datetime(2026, 10, 22, hour, minute, tzinfo=UTC),
    }


def test_sums_only_impacted_customers_inside_window() -> None:
    books = _books(
        [
            _refund("buy_0001", 7000, "05:05"),  # 포함
            _refund("buy_0002", 3000, "05:29"),  # 포함
            _refund("buy_0999", 50000, "05:10"),  # 영향 고객 아님
            _refund("buy_0001", 90000, "04:59"),  # 구간 전
            _refund("buy_0002", 80000, "05:31"),  # 구간 후
        ]
    )
    assert incident_refunds(books, WINDOW, IMPACT) == {"total": 10000, "count": 2}


def test_boundaries_are_inclusive() -> None:
    books = _books([_refund("buy_0001", 1000, "05:00"), _refund("buy_0002", 2000, "05:30")])
    assert incident_refunds(books, WINDOW, IMPACT) == {"total": 3000, "count": 2}


def test_no_refund_rows_is_zero_not_null() -> None:
    """귀속할 수 있는데 해당 환불이 없으면 0원이다 (모르는 것과 구분한다)."""
    assert incident_refunds(_books([]), WINDOW, IMPACT) == {"total": 0, "count": 0}


def test_null_when_window_or_impact_missing() -> None:
    """구간이나 영향 고객을 모르면 귀속하지 않는다 (fail-closed)."""
    books = _books([_refund("buy_0001", 7000, "05:05")])
    assert incident_refunds(books, None, IMPACT) is None
    assert incident_refunds(books, WINDOW, frozenset()) is None
    assert incident_refunds(None, WINDOW, IMPACT) is None


def test_ignores_rows_without_time() -> None:
    row = _refund("buy_0001", 5000, "05:05") | {"created_at": None}
    assert incident_refunds(_books([row]), WINDOW, IMPACT) == {"total": 0, "count": 0}
