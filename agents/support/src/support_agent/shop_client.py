"""shop-api 연결: 클레임 판정에 쓰는 주문 조회와 현금 환불 (REST).

- 주문 조회 `GET /api/shop/orders/{id}`는 토큰이 필요 없다.
  환불 `POST /api/shop/payments/{id}/refund`는 `support` 토큰만 받는다
  (`Authorization: Bearer`, 요청자는 토큰에서 꺼낸다).
- 주소는 `AX_SHOP_API_URL`(없으면 `http://shop-api:8010`, 템플릿 `agent_url`)이다.
- 주문 출처는 `SUPPORT_ORDER_SOURCE`(`seed` | `shop-api`)로 정한다. 비우면 목 모드는 시드 주문,
  그 밖에는 shop-api다. 목 모드에서 실제 shop-api와 맞춰 볼 때만 `shop-api`로 둔다.
- 환불은 `execute("refund_request")`가 `executed`를 돌려준 뒤에만 부른다. 같은 `idempotency_key`의
  재요청은 shop-api가 이전 환불을 돌려주므로 다시 불러도 중복 지급되지 않는다.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import httpx

from ax_agent_template import config
from support_agent import orders
from support_agent.orders import Order, OrderItem

TIMEOUT_SECONDS = 10.0
_client: httpx.AsyncClient | None = None  # 테스트가 `use_client`로 바꿔 끼운다


class ShopError(Exception):
    """shop-api가 거절했거나 응답이 없다. `code`는 shop-api 오류 코드(연결 실패는 UNAVAILABLE)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def use_client(client: httpx.AsyncClient | None) -> None:
    """HTTP 클라이언트를 바꿔 끼운다 (테스트용). None이면 호출마다 새로 만든다."""
    global _client
    _client = client


def order_source() -> str:
    """`seed` 또는 `shop-api`."""
    explicit = os.getenv("SUPPORT_ORDER_SOURCE", "").strip().lower()
    if explicit in ("seed", "shop-api"):
        return explicit
    if _client is not None:
        return "shop-api"
    return "seed" if config.is_mock("support") else "shop-api"


def _url(path: str) -> str:
    return f"{config.agent_url('shop-api')}/api/shop{path}"


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.agent_token('support')}"}


async def _send(method: str, path: str, **kwargs: Any) -> httpx.Response:
    client = _client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        return await client.request(method, _url(path), headers=_headers(), **kwargs)
    except httpx.HTTPError as exc:
        raise ShopError("UNAVAILABLE", f"shop-api에 연결할 수 없다 ({type(exc).__name__})") from exc
    finally:
        if _client is None:
            await client.aclose()


def _error(response: httpx.Response) -> ShopError:
    try:
        error = response.json()["error"]
        return ShopError(str(error["code"]), str(error.get("message", "")))
    except (ValueError, KeyError, TypeError):
        return ShopError("UNAVAILABLE", f"shop-api 응답 오류 ({response.status_code})")


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def to_order(data: dict[str, Any]) -> Order | None:
    """shop-api 주문을 판정용 `Order`로 바꾼다. 결제가 끝나지 않은 주문은 None이다."""
    paid = [p for p in data.get("payments", []) if p.get("status") == "succeeded"]
    if data.get("status") != "paid" or not paid:
        return None
    payment = paid[-1]
    delivered_at = _moment(data.get("delivered_at"))
    paid_at = _moment(payment.get("created_at")) or _moment(data["created_at"])
    assert paid_at is not None
    return Order(
        id=data["order_id"],
        order_no=data["order_id"],  # shop-api에는 별도 주문 번호가 없다
        buyer_id=data["buyer_id"],
        status="delivered" if delivered_at else "preparing",
        paid_at=paid_at,
        promised_delivery_at=_moment(data.get("promised_delivery_at")),
        shipped_at=_moment(data.get("shipped_at")),
        delivered_at=delivered_at,
        shipping_fee=data.get("shipping_fee", 0),
        points_used=data.get("points_used", 0),
        payment_id=payment["payment_id"],
        items=[
            OrderItem(
                id=item["order_item_id"],
                title=item["title"],
                qty=item["qty"],
                paid_amount=item["paid_amount"],
                points_share=item.get("points_share", 0),
            )
            for item in data.get("items", [])
        ],
    )


async def fetch_order(order_id: str) -> Order | None:
    """주문을 읽는다. 없거나 결제가 끝나지 않았으면 None, 연결 실패는 `ShopError`."""
    response = await _send("GET", f"/orders/{order_id}")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _error(response)
    return to_order(response.json()["data"])


async def load_order(order_id: str) -> Order | None:
    """클레임 판정에 쓸 주문. 출처는 `order_source()`가 정한다."""
    if order_source() == "seed":
        return orders.get_order(order_id)
    return await fetch_order(order_id)


async def refund(
    payment_id: str, amount: int, idempotency_key: str, context_id: str
) -> dict[str, Any]:
    """현금 환불을 반영한다. 같은 키의 재요청은 이전 환불을 돌려준다 (`meta.replayed`)."""
    response = await _send(
        "POST",
        f"/payments/{payment_id}/refund",
        json={
            "refund_amount": amount,
            "idempotency_key": idempotency_key,
            "context_id": context_id,
        },
    )
    if response.status_code not in (200, 201):
        raise _error(response)
    return response.json()["data"]
