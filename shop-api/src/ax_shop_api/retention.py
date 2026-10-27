"""보유기간 삭제(retention). 개인정보처리방침 제4조의 보유기간을 코드로 옮긴다.

- 거래 기록(주문·품목·결제·환불·반품·쿠폰)은 기본 5년, 발송이 끝난 outbox는 90일,
  구매자 행은 90일이 지났고 남은 주문·쿠폰이 없을 때만 지운다. 기준은 각 행의 `created_at`이다.
- 시드 데모 구매자(`seed.SEED_BUYER_IDS`)와 그 구매자의 거래 기록은 지우지 않는다.
- 지우기 전에 개인 식별 정보가 없는 일별 집계(`retention_daily_stats`)만 남긴다.
- 기본은 dry-run(건수만 센다). 실제 삭제(`apply`)는 사람이 켠다. 보고서·로그에는 건수만 담는다.
- 외부에 열린 삭제 경로는 만들지 않는다. 사람 승인 뒤 운영자가 CLI로만 실행한다
  (`python -m ax_shop_api.retention`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ax_shop_api.db import Db
from ax_shop_api.seed import SEED_BUYER_IDS

logger = logging.getLogger(__name__)

ENV_TRANSACTION_DAYS = "SHOP_RETENTION_TRANSACTION_DAYS"
ENV_OUTBOX_DAYS = "SHOP_RETENTION_OUTBOX_DAYS"
ENV_BUYER_DAYS = "SHOP_RETENTION_BUYER_DAYS"
ENV_APPLY = "SHOP_RETENTION_APPLY"
ENV_ENABLED = "SHOP_RETENTION_ENABLED"

DEFAULT_TRANSACTION_DAYS = (
    1825  # 5년(방침 제4조 ③). 모의 거래 적용 여부(제4조 ⑦)는 미확정이라 설정값
)
DEFAULT_OUTBOX_DAYS = 90
DEFAULT_BUYER_DAYS = 90
MAX_DAYS = 36500  # 100년. 이보다 크면 잘못된 값으로 본다(날짜 계산 오버플로 방지)
INTERVAL_S = 86400  # 백그라운드 작업 주기: 하루 한 번

# 삭제 순서(자식 → 부모). 건수 키이기도 하다.
KINDS = (
    "refunds",
    "returns",
    "payments",
    "order_items",
    "orders",
    "coupons",
    "outbox_events",
    "buyers",
)


def _days(name: str, default: int) -> int:
    """일 단위 정수 환경변수. 비었거나 정수가 아니거나 0 이하·상한 초과면 기본값."""
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 0 < value <= MAX_DAYS else default


@dataclass(frozen=True)
class RetentionConfig:
    transaction_days: int = DEFAULT_TRANSACTION_DAYS
    outbox_days: int = DEFAULT_OUTBOX_DAYS
    buyer_days: int = DEFAULT_BUYER_DAYS

    @classmethod
    def from_env(cls) -> RetentionConfig:
        return cls(
            transaction_days=_days(ENV_TRANSACTION_DAYS, DEFAULT_TRANSACTION_DAYS),
            outbox_days=_days(ENV_OUTBOX_DAYS, DEFAULT_OUTBOX_DAYS),
            buyer_days=_days(ENV_BUYER_DAYS, DEFAULT_BUYER_DAYS),
        )


@dataclass
class RetentionReport:
    """건수만 담는다(구매자 ID·금액 없음). `applied=False`면 지울 대상 건수(DB 변경 없음)."""

    applied: bool
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        mode = "apply" if self.applied else "dry-run"
        parts = " ".join(f"{kind}={self.counts.get(kind, 0)}" for kind in KINDS)
        return f"retention {mode}: {parts} total={self.total}"


class SeedBuyerError(Exception):
    """시드 데모 구매자는 지우지 않는다."""


class BuyerNotFoundError(Exception):
    pass


class _DryRun(Exception):
    """dry-run 끝에서 트랜잭션을 되돌리려고 쓰는 내부 신호."""


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class _Step:
    kind: str
    table: str
    source: str  # FROM 절. 지울 표는 별칭 t
    where: str
    date_expr: str  # 집계 기준일(YYYY-MM-DD)
    amount_expr: str = "NULL"


_NOT_SEED = "NOT IN (SELECT value FROM json_each(:seeds))"
_NO_CHILDREN = (
    "NOT EXISTS (SELECT 1 FROM payments x WHERE x.order_id = o.order_id) "
    "AND NOT EXISTS (SELECT 1 FROM refunds x WHERE x.order_id = o.order_id) "
    "AND NOT EXISTS (SELECT 1 FROM returns x WHERE x.order_id = o.order_id)"
)
_ORDER_JOIN = "JOIN orders o ON o.order_id = t.order_id"
_DAY_T = "substr(t.created_at, 1, 10)"
_DAY_O = "substr(o.created_at, 1, 10)"


def _expired_steps() -> list[_Step]:
    own = f"t.created_at <= :c_tx AND o.buyer_id {_NOT_SEED}"
    order_gone = f"o.created_at <= :c_tx AND o.buyer_id {_NOT_SEED} AND {_NO_CHILDREN}"
    return [
        _Step("refunds", "refunds", f"refunds t {_ORDER_JOIN}", own, _DAY_T, "t.amount"),
        _Step("returns", "returns", f"returns t {_ORDER_JOIN}", own, _DAY_T),
        _Step(
            "payments",
            "payments",
            f"payments t {_ORDER_JOIN}",
            f"{own} AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.payment_id = t.payment_id)",
            _DAY_T,
            "t.amount",
        ),
        _Step("order_items", "order_items", f"order_items t {_ORDER_JOIN}", order_gone, _DAY_O),
        _Step(
            "orders",
            "orders",
            "orders t JOIN orders o ON o.order_id = t.order_id",
            order_gone,
            _DAY_T,
            "t.total_amount",
        ),
        _Step(
            "coupons",
            "coupons",
            "coupons t",
            f"t.created_at <= :c_tx AND t.buyer_id {_NOT_SEED}",
            _DAY_T,
            "t.total_value",
        ),
        _Step(
            "outbox_events",
            "outbox_events",
            "outbox_events t",
            "t.sent_at IS NOT NULL AND t.created_at <= :c_outbox",
            _DAY_T,
        ),
        _Step(
            "buyers",
            "buyers",
            "buyers t",
            f"t.created_at <= :c_buyer AND t.buyer_id {_NOT_SEED} "
            "AND NOT EXISTS (SELECT 1 FROM orders x WHERE x.buyer_id = t.buyer_id) "
            "AND NOT EXISTS (SELECT 1 FROM coupons x WHERE x.buyer_id = t.buyer_id)",
            _DAY_T,
        ),
    ]


def _buyer_steps(include_retained: bool) -> list[_Step]:
    mine = "o.buyer_id = :buyer"
    steps: list[_Step] = [
        # 발송이 끝난 이 구매자 관련 이벤트(주문을 지우기 전에 주문 번호로 찾는다)
        _Step(
            "outbox_events",
            "outbox_events",
            "outbox_events t",
            "t.sent_at IS NOT NULL AND ("
            "json_extract(t.payload, '$.data.buyer_id') = :buyer "
            "OR json_extract(t.payload, '$.data.order_id') "
            "IN (SELECT order_id FROM orders WHERE buyer_id = :buyer))",
            _DAY_T,
        )
    ]
    if include_retained:
        steps += [
            _Step("refunds", "refunds", f"refunds t {_ORDER_JOIN}", mine, _DAY_T, "t.amount"),
            _Step("returns", "returns", f"returns t {_ORDER_JOIN}", mine, _DAY_T),
            _Step("payments", "payments", f"payments t {_ORDER_JOIN}", mine, _DAY_T, "t.amount"),
            _Step("order_items", "order_items", f"order_items t {_ORDER_JOIN}", mine, _DAY_O),
            _Step(
                "orders",
                "orders",
                "orders t JOIN orders o ON o.order_id = t.order_id",
                mine,
                _DAY_T,
                "t.total_amount",
            ),
            _Step(
                "coupons", "coupons", "coupons t", "t.buyer_id = :buyer", _DAY_T, "t.total_value"
            ),
        ]
    steps.append(
        _Step(
            "buyers",
            "buyers",
            "buyers t",
            "t.buyer_id = :buyer "
            "AND NOT EXISTS (SELECT 1 FROM orders x WHERE x.buyer_id = t.buyer_id) "
            "AND NOT EXISTS (SELECT 1 FROM coupons x WHERE x.buyer_id = t.buyer_id)",
            _DAY_T,
        )
    )
    return steps


def _run_step(conn, step: _Step, params: dict[str, object]) -> int:
    """집계(개인 식별 정보 없음)를 남기고 지운다. 지운(또는 지울) 건수를 돌려준다."""
    conn.execute(
        "INSERT INTO retention_daily_stats(stat_date, kind, rows_count, amount_sum, tenant_id) "
        f"SELECT {step.date_expr}, '{step.kind}', COUNT(*), "  # noqa: S608 (상수 문자열)
        f"CASE WHEN COUNT({step.amount_expr}) = 0 THEN NULL ELSE SUM({step.amount_expr}) END, "
        f"t.tenant_id FROM {step.source} WHERE {step.where} GROUP BY 1, t.tenant_id "
        "ON CONFLICT(stat_date, kind, tenant_id) DO UPDATE SET "
        "rows_count = rows_count + excluded.rows_count, "
        "amount_sum = CASE WHEN amount_sum IS NULL AND excluded.amount_sum IS NULL THEN NULL "
        "ELSE COALESCE(amount_sum, 0) + COALESCE(excluded.amount_sum, 0) END",
        params,
    )
    cursor = conn.execute(
        f"DELETE FROM {step.table} WHERE rowid IN "  # noqa: S608
        f"(SELECT t.rowid FROM {step.source} WHERE {step.where})",
        params,
    )
    return cursor.rowcount


def _execute(db: Db, steps: list[_Step], params: dict[str, object], apply: bool) -> RetentionReport:
    counts = dict.fromkeys(KINDS, 0)
    try:
        with db.tx() as conn:
            for step in steps:
                counts[step.kind] += _run_step(conn, step, params)
            if not apply:
                raise _DryRun  # 같은 삭제를 해 본 뒤 되돌려 건수만 얻는다(DB 변경 없음)
    except _DryRun:
        pass
    return RetentionReport(applied=apply, counts=counts)


def purge_expired(
    db: Db,
    now: datetime,
    *,
    apply: bool = False,
    config: RetentionConfig | None = None,
) -> RetentionReport:
    """보유기간이 지난 행을 지운다. 기본은 dry-run(건수만). `now`는 시간대 있는 시각."""
    cfg = config or RetentionConfig.from_env()
    params: dict[str, object] = {
        "c_tx": _stamp(now - timedelta(days=cfg.transaction_days)),
        "c_outbox": _stamp(now - timedelta(days=cfg.outbox_days)),
        "c_buyer": _stamp(now - timedelta(days=cfg.buyer_days)),
        "seeds": json.dumps(sorted(SEED_BUYER_IDS)),
    }
    return _execute(db, _expired_steps(), params, apply)


def purge_buyer(
    db: Db,
    buyer_id: str,
    *,
    apply: bool = False,
    include_retained: bool = False,
) -> RetentionReport:
    """사람 승인 뒤 운영자가 쓰는 집행부(삭제 요청). 기본은 dry-run.

    기본은 법정 보존 대상(주문·품목·결제·환불·반품·쿠폰)을 남기고, 발송이 끝난 관련 outbox와
    (남은 주문·쿠폰이 없을 때) 구매자 행만 지운다. `include_retained`면 거래 기록까지 지운다.
    """
    if buyer_id in SEED_BUYER_IDS:
        raise SeedBuyerError
    with db.tx() as conn:
        found = conn.execute("SELECT 1 FROM buyers WHERE buyer_id = ?", (buyer_id,)).fetchone()
    if found is None:
        raise BuyerNotFoundError
    return _execute(db, _buyer_steps(include_retained), {"buyer": buyer_id}, apply)


# --- 백그라운드(하루 한 번) ---------------------------------------------------


def _truthy_default_on(name: str) -> bool:
    return (os.getenv(name) or "1").strip().lower() not in ("0", "false", "off", "no")


def enabled(db: Db) -> bool:
    """DB가 파일이고 `SHOP_RETENTION_ENABLED`(기본 1)가 꺼져 있지 않을 때만 켠다."""
    return db.is_file and _truthy_default_on(ENV_ENABLED)


def apply_requested() -> bool:
    """`SHOP_RETENTION_APPLY=1`일 때만 실제 삭제. 그 밖은 dry-run."""
    return (os.getenv(ENV_APPLY) or "").strip() == "1"


def run_once(db: Db, now: datetime | None = None) -> RetentionReport | None:
    """한 번 돌리고 건수만 로그로 남긴다. 실패해도 예외를 밖으로 내지 않는다(종류만 로그)."""
    try:
        report = purge_expired(db, now or datetime.now(UTC), apply=apply_requested())
    except Exception as error:  # noqa: BLE001  백그라운드 작업이 앱을 죽이지 않게 한다
        logger.warning("retention failed: %s", type(error).__name__)
        return None
    logger.info(report.summary())
    return report


async def _loop(db: Db, interval: float) -> None:
    while True:
        await asyncio.to_thread(run_once, db)
        await asyncio.sleep(interval)


def start_background(db: Db, interval: float = INTERVAL_S) -> asyncio.Task[None] | None:
    """앱 시작 시 부른다. 켜지지 않는 조건이면 None."""
    if not enabled(db):
        return None
    return asyncio.create_task(_loop(db, interval))


# --- CLI ---------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ax_shop_api.retention",
        description="보유기간 삭제. 기본은 dry-run(건수만), --apply를 줘야 실제로 지운다.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="보유기간이 지난 행을 정리한다")
    run.add_argument("--apply", action="store_true", help="실제로 삭제한다(되돌릴 수 없다)")
    buyer = sub.add_parser("purge-buyer", help="사람 승인 뒤 한 구매자의 삭제를 집행한다")
    buyer.add_argument("buyer_id")
    buyer.add_argument("--apply", action="store_true", help="실제로 삭제한다(되돌릴 수 없다)")
    buyer.add_argument(
        "--include-retained",
        action="store_true",
        help="법정 보존 대상(주문·결제·환불·반품·쿠폰)까지 지운다",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = Db()
    if not db.is_file:
        print("SHOP_DB_PATH가 파일이 아니라 삭제할 대상이 없다", file=sys.stderr)
        return 2
    try:
        if args.command == "run":
            report = purge_expired(db, datetime.now(UTC), apply=args.apply)
        else:
            report = purge_buyer(
                db, args.buyer_id, apply=args.apply, include_retained=args.include_retained
            )
    except SeedBuyerError:
        print("시드 데모 구매자는 삭제할 수 없다", file=sys.stderr)
        return 2
    except BuyerNotFoundError:
        print("해당 구매자를 찾을 수 없다", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
