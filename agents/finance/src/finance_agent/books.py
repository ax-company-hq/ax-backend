"""재무 장부 자료 모으기: 주문·결제·환불·쿠폰·판매자·클레임·관문 기록·기준표 + 목 PG 정산.

- 목 모드(`FINANCE_MOCK=1` 또는 `AGENT_TEMPLATE_MOCK=1`): 패키지 시드 `finance_books_seed.json`.
  시드의 날짜는 오늘(UTC) 기준 오프셋(`day`)이라 언제 띄워도 최근 2주치가 보인다. 금액은 고정이다.
- 실제 모드: shop-api `GET /api/shop/orders`·`/products`·`/sellers`·`/coupons`(재무 토큰),
  safety `GET /api/safety/thresholds`·`/api/safety/executions`. 판매 사이트 DB에는 붙지 않는다.
- 못 받은 자료는 비워 두고 `unavailable`에 이름을 적는다. 추정값을 만들지 않는다(fail-closed).
- PG 정산은 실제 PG가 없어서 두 모드 모두 결제에서 **토스 정산 형식 목**을 만든다
  (`amount`, `fee`, `pay_out_amount`, `sold_date`, `paid_out_date`). 데모용 불일치를 일부러 넣는다.

모든 계산은 코드가 한다. 이 모듈은 자료를 같은 모양으로 맞추기만 한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import resources
from typing import Any

import httpx

from ax_agent_template.config import agent_token, agent_url, is_mock
from finance_agent import finance_config as fc

CALL_SECONDS = 5.0
SEED_FILE = "finance_books_seed.json"


@dataclass
class Books:
    """한 번 모은 장부 자료. 금액은 정수 원, 시각은 UTC datetime."""

    orders: list[dict[str, Any]] = field(default_factory=list)
    sellers: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    coupons: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] | None = None
    executions: list[dict[str, Any]] | None = None
    thresholds: dict[str, Any] | None = None
    risky_sellers: set[str] = field(default_factory=set)
    pg_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: str = "seed"
    mock: bool = True
    collected_at: datetime = field(default_factory=fc.now)
    unavailable: list[str] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)

    # ---- 찾기
    def seller_name(self, seller_id: str) -> str:
        for s in self.sellers:
            if s.get("seller_id") == seller_id:
                return str(s.get("name") or seller_id)
        return seller_id

    def order(self, order_id: str) -> dict[str, Any] | None:
        return next((o for o in self.orders if o["order_id"] == order_id), None)

    def payments(self) -> list[dict[str, Any]]:
        """성공한 결제 목록 (주문 정보를 붙여서)."""
        rows = []
        for o in self.orders:
            for p in o.get("payments", []):
                if p.get("status") == "succeeded" and isinstance(p.get("amount"), int):
                    rows.append(p | {"order": o})
        return rows

    def paid_by_order(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.payments():
            oid = p["order"]["order_id"]
            out[oid] = out.get(oid, 0) + p["amount"]
        return out

    def meta(self, extra_unavailable: list[str] | None = None) -> dict[str, Any]:
        missing = list(dict.fromkeys([*self.unavailable, *(extra_unavailable or [])]))
        meta: dict[str, Any] = {
            "mock": self.mock,
            "collected_at": fc.iso(self.collected_at),
            "source": self.source,
            "unavailable": missing,
        }
        if self.notes:
            meta["notes"] = dict(self.notes)
        return meta


# ---------------------------------------------------------------- 공통 변환
def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def item_gross(item: dict[str, Any]) -> int:
    return (_int(item.get("qty")) or 0) * (_int(item.get("unit_price")) or 0)


def order_sold(order: dict[str, Any]) -> bool:
    """성공한 결제가 있는 주문만 매출로 본다."""
    return any(p.get("status") == "succeeded" for p in order.get("payments", []))


def in_range(dt: datetime | None, start: datetime, end: datetime) -> bool:
    return dt is not None and start <= dt < end


# ---------------------------------------------------------------- 목 (시드)
def _at(today: date, day: int, hhmm: str) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return fc.day_start(today + timedelta(days=day)) + timedelta(hours=hour, minutes=minute)


def read_seed() -> dict[str, Any]:
    return json.loads(resources.files("finance_agent").joinpath(SEED_FILE).read_text("utf-8"))


def seed_books(today: date | None = None, seed: dict[str, Any] | None = None) -> Books:
    """시드를 오늘 기준 날짜로 펼친다. 같은 날짜면 결과가 항상 같다."""
    today = today or fc.today()
    seed = seed if seed is not None else read_seed()
    refunds_by_order: dict[str, int] = {}
    refunds = []
    for r in seed.get("refunds", []):
        row = {k: v for k, v in r.items() if k not in ("day", "time")}
        row["created_at"] = _at(today, r["day"], r["time"])
        refunds.append(row)
        refunds_by_order[r["order_id"]] = refunds_by_order.get(r["order_id"], 0) + r["amount"]

    orders = []
    for o in seed.get("orders", []):
        created = _at(today, o["day"], o["time"])
        items = [
            i | {"paid_amount": item_gross(i) - (_int(i.get("points_share")) or 0)}
            for i in o["items"]
        ]
        total = sum(i["paid_amount"] for i in items) + o.get("shipping_fee", 0)
        delivered = o.get("delivered_day")
        orders.append(
            {
                "order_id": o["order_id"],
                "buyer_id": o["buyer_id"],
                "status": "paid",
                "total_amount": total,
                "refunded_amount": refunds_by_order.get(o["order_id"], 0),
                "shipping_fee": o.get("shipping_fee", 0),
                "points_used": o.get("points_used", 0),
                "created_at": created,
                "delivered_at": _at(today, delivered, "08:00") if delivered is not None else None,
                "items": items,
                "payments": [
                    {
                        "payment_id": o["payment_id"],
                        "amount": total,
                        "method_code": "card",
                        "status": "succeeded",
                        "created_at": created,
                    }
                ],
            }
        )

    coupons = []
    for c in seed.get("coupons", []):
        row = {k: v for k, v in c.items() if k not in ("day", "time")}
        row["created_at"] = _at(today, c["day"], c["time"])
        coupons.append(row)

    claims = []
    for c in seed.get("claims", []):
        row = {k: v for k, v in c.items() if k != "day"}
        row["created_at"] = _at(today, c["day"], "12:00")
        claims.append(row)

    executions = []
    for e in seed.get("executions", []):
        executions.append(
            {
                "execution_id": e["execution_id"],
                "created_at": _at(today, e["day"], e["time"]),
                "action": e["action"],
                "requester": e["requester"],
                "context_id": e["context_id"],
                "params": e["params"],
                "status": e["status"],
                "grade": e["grade"],
                "hold_reason": e.get("hold_reason"),
                "computed_amount": e.get("computed_amount"),
                "review_id": e.get("review_id"),
            }
        )

    return Books(
        orders=orders,
        sellers=list(seed.get("sellers", [])),
        refunds=refunds,
        coupons=coupons,
        claims=claims,
        executions=executions,
        thresholds=seed.get("thresholds"),
        risky_sellers=set(seed.get("risky_sellers", [])),
        pg_overrides=dict(seed.get("pg_overrides", {})),
        source="seed",
        mock=True,
        collected_at=fc.now(),
    )


# ---------------------------------------------------------------- 실제 (shop-api, safety)
async def _get(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> Any:
    """`{data, meta}` 응답의 data. 실패하면 None."""
    try:
        response = await client.get(url, headers=headers or {}, timeout=CALL_SECONDS)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("data") if isinstance(body, dict) else None


def normalize_order(raw: dict[str, Any], categories: dict[str, str]) -> dict[str, Any] | None:
    """shop-api 주문 응답 → 장부 주문. 형식이 다르면 None (그 주문은 빼고 unavailable에 적는다)."""
    created = fc.parse_dt(raw.get("created_at"))
    items, payments = raw.get("items"), raw.get("payments")
    if not isinstance(raw.get("order_id"), str) or created is None:
        return None
    if not isinstance(items, list) or not isinstance(payments, list):
        return None
    norm_items = []
    for i in items:
        if not isinstance(i, dict) or _int(i.get("qty")) is None:
            return None
        if _int(i.get("unit_price")) is None:
            return None
        paid = _int(i.get("paid_amount"))
        norm_items.append(
            i
            | {
                "category_code": i.get("category_code") or categories.get(str(i.get("product_id"))),
                "paid_amount": paid if paid is not None else item_gross(i),
            }
        )
    norm_payments = []
    for p in payments:
        if not isinstance(p, dict):
            return None
        norm_payments.append(p | {"created_at": fc.parse_dt(p.get("created_at")) or created})
    return {
        "order_id": raw["order_id"],
        "buyer_id": raw.get("buyer_id"),
        "status": raw.get("status"),
        "total_amount": _int(raw.get("total_amount")) or 0,
        "refunded_amount": _int(raw.get("refunded_amount")) or 0,
        "shipping_fee": _int(raw.get("shipping_fee")) or 0,
        "points_used": _int(raw.get("points_used")) or 0,
        "created_at": created,
        "delivered_at": fc.parse_dt(raw.get("delivered_at")),
        "items": norm_items,
        "payments": norm_payments,
    }


def refunds_from_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """shop-api 주문의 `refunded_amount`를 환불 기록으로 옮긴다.

    shop-api 주문 응답에는 환불 시각이 없어서 주문 시각으로 둔다(`notes.refund_basis`).
    판매자는 품목 결제 금액 비율로 나눈다(나머지는 마지막 품목).
    """
    rows = []
    for o in orders:
        amount = o["refunded_amount"]
        if not amount:
            continue
        paid_items = [i for i in o["items"] if i.get("paid_amount")]
        total_paid = sum(i["paid_amount"] for i in paid_items) or 1
        left = amount
        for idx, item in enumerate(paid_items):
            share = (
                left if idx == len(paid_items) - 1 else amount * item["paid_amount"] // total_paid
            )
            left -= share
            if share:
                rows.append(
                    {
                        "refund_id": f"rfd_of_{o['order_id']}_{idx}",
                        "order_id": o["order_id"],
                        "payment_id": next(
                            (p["payment_id"] for p in o["payments"] if p.get("payment_id")), None
                        ),
                        "buyer_id": o.get("buyer_id"),
                        "seller_id": item.get("seller_id"),
                        "amount": share,
                        "created_at": o["created_at"],
                        "execution_id": None,
                    }
                )
    return rows


def normalize_execution(raw: dict[str, Any]) -> dict[str, Any] | None:
    """safety `ExecutionRecord` → 장부 관문 기록."""
    request, decision = raw.get("request"), raw.get("decision")
    created = fc.parse_dt(raw.get("created_at"))
    if not isinstance(request, dict) or not isinstance(decision, dict) or created is None:
        return None
    return {
        "execution_id": raw.get("execution_id"),
        "created_at": created,
        "action": request.get("action"),
        "requester": request.get("requester"),
        "context_id": request.get("context_id"),
        "params": request.get("params") or {},
        "status": decision.get("status"),
        "grade": decision.get("grade"),
        "hold_reason": decision.get("hold_reason"),
        "computed_amount": decision.get("computed_amount"),
        "review_id": decision.get("review_id"),
    }


async def live_books(http_client: httpx.AsyncClient | None = None) -> Books:
    client = http_client or httpx.AsyncClient()
    unavailable: list[str] = []
    try:
        shop, safety = agent_url("shop-api"), agent_url("safety")
        orders_raw = await _get(client, f"{shop}/api/shop/orders")
        products = await _get(client, f"{shop}/api/shop/products")
        sellers = await _get(client, f"{shop}/api/shop/sellers")
        try:
            headers = {"Authorization": f"Bearer {agent_token('finance')}"}
        except RuntimeError:
            headers = {}
        coupons_raw = await _get(client, f"{shop}/api/shop/coupons", headers) if headers else None
        thresholds = await _get(client, f"{safety}/api/safety/thresholds")
        executions_raw = await _get(client, f"{safety}/api/safety/executions")
    finally:
        if http_client is None:
            await client.aclose()

    categories = {
        str(p.get("product_id")): str(p.get("category_code"))
        for p in (products if isinstance(products, list) else [])
        if isinstance(p, dict)
    }
    orders: list[dict[str, Any]] = []
    if isinstance(orders_raw, list):
        skipped = 0
        for raw in orders_raw:
            norm = normalize_order(raw, categories) if isinstance(raw, dict) else None
            if norm is None:
                skipped += 1
            else:
                orders.append(norm)
        if skipped:
            unavailable.append("orders_partial")
    else:
        unavailable.append("orders")
    if not isinstance(products, list):
        unavailable.append("products")
    if not isinstance(sellers, list):
        unavailable.append("sellers")
    coupons = []
    if isinstance(coupons_raw, list):
        for c in coupons_raw:
            if isinstance(c, dict):
                coupons.append(
                    c
                    | {
                        "created_at": fc.parse_dt(c.get("created_at")),
                        "funded_by": c.get("funded_by") or "company",
                        "seller_id": c.get("seller_id"),
                    }
                )
    else:
        unavailable.append("coupons")
    executions = None
    if isinstance(executions_raw, list):
        executions = [e for e in map(_norm_exec, executions_raw) if e is not None]
    else:
        unavailable.append("executions")
    if not isinstance(thresholds, dict):
        thresholds = None
        unavailable.append("thresholds")
    unavailable.append(
        "claims"
    )  # shop-api에 클레임 목록 조회가 없다 → 처리 중 클레임은 확인할 수 없음
    return Books(
        orders=orders,
        sellers=sellers if isinstance(sellers, list) else [],
        refunds=refunds_from_orders(orders),
        coupons=coupons,
        claims=None,
        executions=executions,
        thresholds=thresholds,
        source="shop-api",
        mock=False,
        collected_at=fc.now(),
        unavailable=unavailable,
        notes={"refund_basis": "order_created_at"},
    )


def _norm_exec(raw: Any) -> dict[str, Any] | None:
    return normalize_execution(raw) if isinstance(raw, dict) else None


async def load_books() -> Books:
    """모드에 맞게 장부를 모은다."""
    if is_mock("finance"):
        return seed_books()
    return await live_books()


# ---------------------------------------------------------------- 목 PG 정산 (토스 정산 형식)
def _live_overrides(settled_ids: list[str]) -> dict[str, dict[str, Any]]:
    """실제 모드에서 데모 불일치를 넣을 결제. 결제 번호 순서의 정해진 자리라 매번 같다."""
    plan = {3: {"kind": "mismatch", "delta": -2000}, 7: {"kind": "missing"}}
    plan |= {11: {"kind": "fee", "fee_rate": 0.035}, 13: {"kind": "mismatch", "delta": 1000}}
    ordered = sorted(settled_ids)
    return {ordered[i]: spec for i, spec in plan.items() if i < len(ordered)}


def pg_settlements(books: Books, today: date | None = None) -> dict[str, dict[str, Any]]:
    """결제별 목 PG 정산 기록. PG에 없는 결제는 키가 없다.

    - `sold_date` = 결제일, `paid_out_date` = 판매일 + `payout_days`(지급 예정일)
    - 지급 예정일이 오늘 이후면 아직 정산 전(`paid_out_date`는 예정일, `settled` False)
    """
    cfg = fc.load_config().pg
    today = today or fc.today()
    payments = books.payments()
    settled_ids = [
        p["payment_id"]
        for p in payments
        if p["created_at"].date() + timedelta(days=cfg.payout_days) <= today
    ]
    overrides = books.pg_overrides if books.mock else _live_overrides(settled_ids)
    out: dict[str, dict[str, Any]] = {}
    for p in payments:
        sold = p["created_at"].date()
        payout = sold + timedelta(days=cfg.payout_days)
        settled = payout <= today
        spec = overrides.get(p["payment_id"], {}) if settled else {}
        if spec.get("kind") == "missing":
            continue
        amount = (
            p["amount"] + int(spec.get("delta", 0))
            if spec.get("kind") == "mismatch"
            else p["amount"]
        )
        fee = fc.rate_amount(amount, float(spec.get("fee_rate", cfg.fee_rate)))
        out[p["payment_id"]] = {
            "payment_id": p["payment_id"],
            "order_id": p["order"]["order_id"],
            "amount": amount,
            "fee": fee,
            "pay_out_amount": amount - fee,
            "sold_date": sold.isoformat(),
            "paid_out_date": payout.isoformat(),
            "settled": settled,
        }
    return out
