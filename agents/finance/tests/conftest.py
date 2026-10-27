"""재무 재설계 모듈 테스트 공용: 날짜 고정, 작은 장부 만들기."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent import finance_config as fc
from finance_agent.books import Books

# 테스트에서는 재무 AI 자동 테스트(백그라운드 루프)를 끈다. 켜 보는 테스트는 monkeypatch로 직접 켠다
os.environ["FINANCE_SELF_TEST_SECONDS"] = "0"
# 매일 결산 자동 실행도 끈다 (켜 보는 테스트는 monkeypatch로 직접 켠다)
os.environ["FINANCE_DAILY_CLOSING_SECONDS"] = "0"

FIXED_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)  # 금요일. 지난주 = 2026-W42 (10-12~10-18)


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(fc, "now", lambda: FIXED_NOW)
    return FIXED_NOW


def at(day: str, hhmm: str = "10:00") -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(hours=hour, minutes=minute)


def order(
    order_id: str,
    when: datetime,
    items: list[tuple[str, str, int, int]],
    *,
    buyer: str = "buy_0001",
    shipping: int = 0,
    delivered: bool = True,
    payment_status: str = "succeeded",
) -> dict[str, Any]:
    """items: (seller_id, category_code, qty, unit_price)."""
    rows = [
        {
            "product_id": f"prd_{n}",
            "seller_id": seller,
            "category_code": cat,
            "qty": qty,
            "unit_price": price,
            "points_share": 0,
            "paid_amount": qty * price,
        }
        for n, (seller, cat, qty, price) in enumerate(items)
    ]
    total = sum(r["paid_amount"] for r in rows) + shipping
    return {
        "order_id": order_id,
        "buyer_id": buyer,
        "status": "paid",
        "total_amount": total,
        "refunded_amount": 0,
        "shipping_fee": shipping,
        "points_used": 0,
        "created_at": when,
        "delivered_at": when + timedelta(days=1) if delivered else None,
        "items": rows,
        "payments": [
            {
                "payment_id": order_id.replace("ord_", "pay_"),
                "amount": total,
                "status": payment_status,
                "created_at": when,
            }
        ],
    }


def books(**kw: Any) -> Books:
    base: dict[str, Any] = {
        "sellers": [
            {"seller_id": "sel_a", "name": "가게A"},
            {"seller_id": "sel_b", "name": "가게B"},
        ],
        "claims": [],
        "executions": [],
        "thresholds": {
            "rules": [
                {"key": "coupon_unit", "action": "issue_coupon", "metric": "unit", "value": 20000},
                {
                    "key": "refund_single",
                    "action": "refund_request",
                    "metric": "amount",
                    "value": 100000,
                },
            ]
        },
        "mock": True,
        "source": "seed",
    }
    return Books(**(base | kw))


@pytest.fixture
def kit() -> SimpleNamespace:
    """테스트 파일에서 쓰는 도우미 묶음 (importlib 모드라 conftest를 import하지 않는다)."""
    return SimpleNamespace(at=at, order=order, books=books, now=FIXED_NOW)
