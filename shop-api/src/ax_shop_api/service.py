"""상품·주문·가짜 결제 업무 규칙. 웹 프레임워크에 의존하지 않는다.

- 가격·총액은 DB 값으로 코드가 계산한다(요청 값을 믿지 않는다).
- 주문·결제·outbox 이벤트는 한 트랜잭션에 저장한다.
- 가짜 결제는 `Gateway`가 성공·실패를 정한다. 장애 주입(SHOP-06)은 이 자리에 붙는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ax_contracts.schemas import EventType, PaymentFailedData
from ax_shop_api import outbox
from ax_shop_api.db import TENANT_ID, Db

CANCELLABLE = ("pending_payment", "payment_failed")


class ShopError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class PaymentOutcome:
    ok: bool
    failure_code: str | None = None


class Gateway:
    """가짜 결제 서버. 요청한 대로(`simulate`) 성공·실패를 돌려준다. 장애 주입은 여기를 바꾼다."""

    def attempt(self, simulate: str | None) -> PaymentOutcome:
        if simulate == "failure":
            return PaymentOutcome(False, "PG_DECLINED")
        return PaymentOutcome(True)


class Shop:
    def __init__(self, db: Db, gateway: Gateway | None = None) -> None:
        self.db = db
        self.gateway = gateway or Gateway()

    # --- 조회 -------------------------------------------------------------
    def list_sellers(self) -> list[dict[str, Any]]:
        with self.db.tx() as conn:
            rows = conn.execute("SELECT * FROM sellers ORDER BY seller_id").fetchall()
        return [
            {
                "seller_id": r["seller_id"],
                "name": r["name"],
                "category_codes": r["category_codes"].split(","),
            }
            for r in rows
        ]

    def list_products(
        self, seller_id: str | None = None, category_code: str | None = None
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM products WHERE active = 1", []
        for column, value in (("seller_id", seller_id), ("category_code", category_code)):
            if value:
                sql += f" AND {column} = ?"
                args.append(value)
        with self.db.tx() as conn:
            rows = conn.execute(sql + " ORDER BY product_id", args).fetchall()
        return [self._product(r) for r in rows]

    def get_product(self, product_id: str) -> dict[str, Any]:
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()
        if row is None:
            raise ShopError("PRODUCT_NOT_FOUND", "상품을 찾을 수 없다", 404)
        return self._product(row)

    @staticmethod
    def _product(row: sqlite3.Row) -> dict[str, Any]:
        keys = ("product_id", "seller_id", "name", "category_code", "price", "stock")
        return {key: row[key] for key in keys} | {"active": bool(row["active"])}

    def get_order(self, order_id: str) -> dict[str, Any]:
        with self.db.tx() as conn:
            return self._order(conn, order_id)

    def list_orders(self, buyer_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.tx() as conn:
            sql, args = "SELECT order_id FROM orders", []
            if buyer_id:
                sql, args = sql + " WHERE buyer_id = ?", [buyer_id]
            ids = [r["order_id"] for r in conn.execute(sql + " ORDER BY order_id", args)]
            return [self._order(conn, order_id) for order_id in ids]

    def _order(self, conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
        order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if order is None:
            raise ShopError("ORDER_NOT_FOUND", "주문을 찾을 수 없다", 404)
        items = conn.execute(
            "SELECT product_id, seller_id, qty, unit_price FROM order_items WHERE order_id = ?",
            (order_id,),
        ).fetchall()
        payments = conn.execute(
            "SELECT payment_id, amount, method_code, status, failure_code FROM payments "
            "WHERE order_id = ? ORDER BY rowid",
            (order_id,),
        ).fetchall()
        return {
            "order_id": order["order_id"],
            "buyer_id": order["buyer_id"],
            "status": order["status"],
            "total_amount": order["total_amount"],
            "created_at": order["created_at"],
            "items": [dict(i) for i in items],
            "payments": [dict(p) for p in payments],
        }

    # --- 상품 관리 ----------------------------------------------------------
    def create_product(
        self, seller_id: str, name: str, category_code: str, price: int, stock: int
    ) -> dict[str, Any]:
        with self.db.tx() as conn:
            if not conn.execute(
                "SELECT 1 FROM sellers WHERE seller_id = ?", (seller_id,)
            ).fetchone():
                raise ShopError("SELLER_NOT_FOUND", "판매자를 찾을 수 없다", 404)
            product_id = self.db.next_id(conn, "product", "prd_")
            now = _now()
            conn.execute(
                "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)",
                (product_id, seller_id, name, category_code, price, stock, TENANT_ID, now, now),
            )
        return self.get_product(product_id)

    def update_product(self, product_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "price", "stock", "active"}
        fields = {k: (int(v) if k == "active" else v) for k, v in changes.items() if k in allowed}
        self.get_product(product_id)
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            with self.db.tx() as conn:
                conn.execute(
                    f"UPDATE products SET {assignments}, updated_at = ? WHERE product_id = ?",
                    [*fields.values(), _now(), product_id],
                )
        return self.get_product(product_id)

    # --- 주문·결제 ----------------------------------------------------------
    def create_order(self, buyer_id: str, lines: list[tuple[str, int]]) -> dict[str, Any]:
        if not lines:
            raise ShopError("EMPTY_ORDER", "주문 상품이 없다")
        with self.db.tx() as conn:
            if not conn.execute("SELECT 1 FROM buyers WHERE buyer_id = ?", (buyer_id,)).fetchone():
                raise ShopError("BUYER_NOT_FOUND", "구매자를 찾을 수 없다", 404)
            items, total = [], 0
            for product_id, qty in lines:
                product = conn.execute(
                    "SELECT * FROM products WHERE product_id = ? AND active = 1", (product_id,)
                ).fetchone()
                if product is None:
                    raise ShopError("PRODUCT_NOT_FOUND", f"상품 {product_id}을 찾을 수 없다", 404)
                if product["stock"] < qty:
                    raise ShopError("OUT_OF_STOCK", f"상품 {product_id}의 재고가 부족하다", 409)
                items.append(
                    {
                        "product_id": product_id,
                        "seller_id": product["seller_id"],
                        "qty": qty,
                        "unit_price": product["price"],
                    }
                )
                total += qty * product["price"]  # 계산은 코드로
                conn.execute(
                    "UPDATE products SET stock = stock - ? WHERE product_id = ?", (qty, product_id)
                )
            order_id = self.db.next_id(conn, "order", "ord_")
            now = _now()
            conn.execute(
                "INSERT INTO orders VALUES (?, ?, 'pending_payment', ?, ?, NULL, ?, ?)",
                (order_id, buyer_id, total, TENANT_ID, now, now),
            )
            for item in items:
                conn.execute(
                    "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (order_id, item["product_id"], item["seller_id"], item["qty"],
                     item["unit_price"], TENANT_ID),
                )  # fmt: skip
            outbox.enqueue(
                conn,
                EventType.ORDER_CREATED,
                {"order_id": order_id, "buyer_id": buyer_id, "items": items, "total_amount": total},
                f"order.created:{order_id}",
            )
        return self.get_order(order_id)

    def pay(self, order_id: str, method_code: str, simulate: str | None) -> dict[str, Any]:
        outcome = self.gateway.attempt(simulate)
        with self.db.tx() as conn:
            order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                raise ShopError("ORDER_NOT_FOUND", "주문을 찾을 수 없다", 404)
            if order["status"] not in ("pending_payment", "payment_failed"):
                raise ShopError("ORDER_NOT_PAYABLE", "결제할 수 없는 주문 상태다", 409)
            payment_id = self.db.next_id(conn, "payment", "pay_")
            now = _now()
            amount = order["total_amount"]
            status = "succeeded" if outcome.ok else "failed"
            conn.execute(
                "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (payment_id, order_id, amount, method_code, status, outcome.failure_code,
                 TENANT_ID, now),
            )  # fmt: skip
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE order_id = ?",
                ("paid" if outcome.ok else "payment_failed", now, order_id),
            )
            if outcome.ok:
                data = {
                    "payment_id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "method_code": method_code,
                }
                outbox.enqueue(
                    conn, EventType.PAYMENT_SUCCEEDED, data, f"payment.succeeded:{payment_id}"
                )
            else:
                failed = PaymentFailedData(
                    payment_id=payment_id,
                    order_id=order_id,
                    amount=amount,
                    failure_code=outcome.failure_code or "UNKNOWN",
                )
                outbox.enqueue(
                    conn,
                    EventType.PAYMENT_FAILED,
                    failed.model_dump(mode="json"),
                    f"payment.failed:{payment_id}",
                )
        return self.get_order(order_id)

    def cancel(self, order_id: str, reason_code: str) -> dict[str, Any]:
        with self.db.tx() as conn:
            order = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                raise ShopError("ORDER_NOT_FOUND", "주문을 찾을 수 없다", 404)
            if order["status"] not in CANCELLABLE:
                raise ShopError(
                    "ORDER_NOT_CANCELLABLE",
                    "결제된 주문의 취소·환불은 환불 API(SHOP-12, 2차)로만 한다",
                    409,
                )
            for item in conn.execute(
                "SELECT product_id, qty FROM order_items WHERE order_id = ?", (order_id,)
            ):
                conn.execute(
                    "UPDATE products SET stock = stock + ? WHERE product_id = ?",
                    (item["qty"], item["product_id"]),
                )
            conn.execute(
                "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE order_id = ?",
                (_now(), order_id),
            )
            outbox.enqueue(
                conn,
                EventType.ORDER_CANCELLED,
                {
                    "order_id": order_id,
                    "reason_code": reason_code,
                    "cancelled_amount": order["total_amount"],
                },
                f"order.cancelled:{order_id}",
            )
        return self.get_order(order_id)
