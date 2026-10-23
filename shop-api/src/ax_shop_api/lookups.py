"""data 에이전트가 읽는 조회 API의 업무 규칙(실패 주문 목록·지표 시계열). 읽기 전용이다.

형식은 `contracts/data-access/`의 `shop_failed_orders`·`shop_metric_series`를 따른다.
지표는 DB에서 코드로 계산한다(LLM 없음). 웹 프레임워크에 의존하지 않는다.

- 실패 주문: 결제가 `failed`인 기록 하나가 한 줄이다. 주문의 `category`는 첫 품목의 상품 분류다.
- 시계열: 5분 버킷, 버킷 시작 시각(UTC)이 `ts`다. 구간은 `from <= 시각 < to`이고 `from`은 버킷
  경계로 내림한다. 건수 지표는 값이 없는 버킷도 0으로 채우고, 비율 지표는 결제 시도가 없는
  버킷을 뺀다(0%와 '측정 못 함'을 섞지 않는다).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ax_contracts.data_access.shop_failed_orders import (
    FailedOrder,
    ShopFailedOrdersQuery,
    ShopFailedOrdersResult,
)
from ax_contracts.data_access.shop_metric_series import (
    MetricPoint,
    ShopMetricSeriesQuery,
    ShopMetricSeriesResult,
)
from ax_shop_api.db import Db
from ax_shop_api.service import ShopError

BUCKET = timedelta(minutes=5)
MAX_POINTS = 2016  # 5분 버킷 7일치. 넘으면 잘라서 `truncated`로 알린다


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _floor(moment: datetime) -> datetime:
    seconds = int(BUCKET.total_seconds())
    return datetime.fromtimestamp(int(moment.timestamp()) // seconds * seconds, UTC)


class Lookups:
    def __init__(self, db: Db, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.db = db
        self.now = now

    def failed_orders(self, query: ShopFailedOrdersQuery) -> ShopFailedOrdersResult:
        if query.to <= query.from_:
            raise ShopError("INVALID_RANGE", "to는 from보다 뒤여야 한다")
        sql = (
            "SELECT p.order_id, o.buyer_id, p.amount, p.failure_code, p.created_at, "
            "  (SELECT pr.category_code FROM order_items i JOIN products pr "
            "   ON pr.product_id = i.product_id WHERE i.order_id = p.order_id "
            "   ORDER BY i.rowid LIMIT 1) AS category "
            "FROM payments p JOIN orders o ON o.order_id = p.order_id "
            "WHERE p.status = 'failed' AND p.tenant_id = ? "
            "AND p.created_at >= ? AND p.created_at < ?"
        )
        args: list[Any] = [query.tenant_id, _iso(query.from_), _iso(query.to)]
        if query.category:
            sql += " AND category = ?"
            args.append(query.category)
        sql += " ORDER BY p.created_at, p.payment_id LIMIT ?"
        args.append(query.limit + 1)  # 하나 더 읽어 잘림을 안다
        with self.db.tx() as conn:
            rows = conn.execute(sql, args).fetchall()
        orders = [
            FailedOrder(
                order_id=row["order_id"],
                buyer_id=row["buyer_id"],
                amount=row["amount"],
                category=row["category"] or "unknown",
                failed_at=_parse(row["created_at"]),
                reason=row["failure_code"] or "UNKNOWN",
                tenant_id=query.tenant_id,
            )
            for row in rows[: query.limit]
        ]
        return ShopFailedOrdersResult(
            orders=orders, collected_at=self.now(), truncated=len(rows) > query.limit
        )

    def metric_series(self, query: ShopMetricSeriesQuery) -> ShopMetricSeriesResult:
        if query.to <= query.from_:
            raise ShopError("INVALID_RANGE", "to는 from보다 뒤여야 한다")
        start = _floor(query.from_)
        with self.db.tx() as conn:
            if query.metric == "order_count":
                rows = conn.execute(
                    "SELECT created_at, 1 AS ok FROM orders "
                    "WHERE tenant_id = ? AND created_at >= ? AND created_at < ?",
                    (query.tenant_id, _iso(start), _iso(query.to)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT created_at, status = 'succeeded' AS ok, order_id FROM payments "
                    "WHERE tenant_id = ? AND created_at >= ? AND created_at < ?",
                    (query.tenant_id, _iso(start), _iso(query.to)),
                ).fetchall()
        buckets: dict[datetime, list[Any]] = defaultdict(list)
        for row in rows:
            buckets[_floor(_parse(row["created_at"]))].append(row)

        points: list[MetricPoint] = []
        truncated = False
        moment = start
        while moment < query.to:
            if len(points) >= MAX_POINTS:
                truncated = True
                break
            value = self._value(query.metric, buckets.get(moment, []))
            if value is not None:
                points.append(MetricPoint(ts=moment, value=value))
            moment += BUCKET
        return ShopMetricSeriesResult(
            metric=query.metric,
            bucket=query.bucket,
            points=points,
            collected_at=self.now(),
            truncated=truncated,
            tenant_id=query.tenant_id,
        )

    @staticmethod
    def _value(metric: str, rows: list[Any]) -> float | None:
        failed = [row for row in rows if not row["ok"]] if metric != "order_count" else []
        if metric == "order_count":
            return float(len(rows))
        if metric == "payment_count":
            return float(len(rows))
        if metric == "order_fail_count":
            return float(len({row["order_id"] for row in failed}))
        if not rows:  # payment_fail_rate: 결제 시도가 없으면 값이 없다
            return None
        return len(failed) / len(rows)
