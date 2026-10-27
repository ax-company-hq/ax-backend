import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ax_shop_api import retention
from ax_shop_api.db import TENANT_ID, Db
from ax_shop_api.main import create_app
from ax_shop_api.retention import (
    BuyerNotFoundError,
    RetentionConfig,
    SeedBuyerError,
    purge_buyer,
    purge_expired,
)
from ax_shop_api.seed import SEED_BUYER_IDS, seed

NOW = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
CFG = RetentionConfig()  # 거래 1825일 · outbox 90일 · 구매자 90일


def ts(days_ago: float, seconds: int = 0) -> str:
    moment = NOW - timedelta(days=days_ago) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SHOP_RETENTION_TRANSACTION_DAYS",
        "SHOP_RETENTION_OUTBOX_DAYS",
        "SHOP_RETENTION_BUYER_DAYS",
        "SHOP_RETENTION_APPLY",
        "SHOP_RETENTION_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db() -> Db:
    return Db(":memory:")


def buyer(db: Db, buyer_id: str, created: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO buyers VALUES (?, ?, NULL, ?)", (buyer_id, TENANT_ID, created))


def order(db: Db, order_id: str, buyer_id: str, created: str, amount: int = 1000) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO orders(order_id, buyer_id, status, total_amount, tenant_id, "
            "created_at, updated_at) VALUES (?, ?, 'paid', ?, ?, ?, ?)",
            (order_id, buyer_id, amount, TENANT_ID, created, created),
        )
        c.execute(
            "INSERT INTO order_items(order_id, product_id, seller_id, qty, unit_price, tenant_id) "
            "VALUES (?, 'prd_0001', 'sel_0001', 1, ?, ?)",
            (order_id, amount, TENANT_ID),
        )


def payment(db: Db, pay_id: str, order_id: str, created: str, amount: int = 1000) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO payments VALUES (?, ?, ?, 'card', 'succeeded', NULL, ?, NULL, ?)",
            (pay_id, order_id, amount, TENANT_ID, created),
        )


def refund(db: Db, ref_id: str, pay_id: str, order_id: str, created: str) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO refunds VALUES (?, ?, ?, 500, ?, ?, NULL, ?)",
            (ref_id, pay_id, order_id, f"k-{ref_id}", TENANT_ID, created),
        )


def ret(db: Db, ret_id: str, claim: int, order_id: str, created: str) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO returns VALUES (?, ?, ?, NULL, ?, ?, NULL, ?)",
            (ret_id, claim, order_id, created, TENANT_ID, created),
        )


def coupon(db: Db, cid: str, buyer_id: str, created: str) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO coupons VALUES (?, ?, 'compensation', NULL, 1, 500, 500, ?, ?, NULL, ?)",
            (cid, buyer_id, f"k-{cid}", TENANT_ID, created),
        )


def outbox_row(db: Db, event_id: str, created: str, sent: bool, payload: str = "{}") -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO outbox_events VALUES (?, ?, ?, NULL, ?, ?)",
            (event_id, payload, TENANT_ID, created, created if sent else None),
        )


def count(db: Db, table: str) -> int:
    with db.tx() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def full_chain(db: Db, suffix: str, buyer_id: str, created: str) -> None:
    order(db, f"ord_{suffix}", buyer_id, created)
    payment(db, f"pay_{suffix}", f"ord_{suffix}", created)
    refund(db, f"ref_{suffix}", f"pay_{suffix}", f"ord_{suffix}", created)
    ret(db, f"ret_{suffix}", int(suffix, 16), f"ord_{suffix}", created)


# --- 기준 일수 ---------------------------------------------------------------


def test_transaction_boundary_one_second_before_and_exactly_five_years(db: Db) -> None:
    buyer(db, "buy_9001", ts(10))
    buyer(db, "buy_9002", ts(10))
    full_chain(db, "01", "buy_9001", ts(1825))  # 정확히 5년 = 만료
    full_chain(db, "02", "buy_9002", ts(1825, seconds=1))  # 1초 모자람 = 보존
    report = purge_expired(db, NOW, apply=True, config=CFG)
    assert report.counts["orders"] == 1 and report.counts["order_items"] == 1
    assert report.counts["payments"] == 1 and report.counts["refunds"] == 1
    assert report.counts["returns"] == 1
    with db.tx() as c:
        assert [r[0] for r in c.execute("SELECT order_id FROM orders")] == ["ord_02"]


def test_default_is_five_years_and_three_months_old_orders_stay(db: Db) -> None:
    buyer(db, "buy_9001", ts(10))
    order(db, "ord_a", "buy_9001", ts(200))  # 3개월 넘었어도 거래 기록은 5년
    report = purge_expired(db, NOW, apply=True, config=RetentionConfig.from_env())
    assert report.total == 0 and count(db, "orders") == 1


def test_coupon_boundary(db: Db) -> None:
    buyer(db, "buy_9001", ts(10))
    coupon(db, "cpn_a", "buy_9001", ts(1825))
    coupon(db, "cpn_b", "buy_9001", ts(1825, seconds=1))
    assert purge_expired(db, NOW, apply=True, config=CFG).counts["coupons"] == 1
    assert count(db, "coupons") == 1


def test_outbox_only_sent_and_past_cutoff(db: Db) -> None:
    outbox_row(db, "evt_old_sent", ts(90), True)
    outbox_row(db, "evt_new_sent", ts(90, seconds=1), True)
    outbox_row(db, "evt_old_unsent", ts(400), False)  # 발송 안 됨 → 보존
    report = purge_expired(db, NOW, apply=True, config=CFG)
    assert report.counts["outbox_events"] == 1
    with db.tx() as c:
        left = {r[0] for r in c.execute("SELECT event_id FROM outbox_events")}
    assert left == {"evt_new_sent", "evt_old_unsent"}


def test_buyer_boundary_and_blocked_by_remaining_orders_or_coupons(db: Db) -> None:
    buyer(db, "buy_9001", ts(90))  # 만료, 남은 것 없음 → 삭제
    buyer(db, "buy_9002", ts(90, seconds=1))  # 1초 모자람 → 보존
    buyer(db, "buy_9003", ts(500))  # 5년 안 주문 있음 → 보존
    buyer(db, "buy_9004", ts(500))  # 5년 안 쿠폰 있음 → 보존
    order(db, "ord_a", "buy_9003", ts(400))
    coupon(db, "cpn_a", "buy_9004", ts(400))
    report = purge_expired(db, NOW, apply=True, config=CFG)
    assert report.counts["buyers"] == 1
    with db.tx() as c:
        left = {r[0] for r in c.execute("SELECT buyer_id FROM buyers")}
    assert left == {"buy_9002", "buy_9003", "buy_9004"}


def test_buyer_deleted_after_orders_expire_in_same_run(db: Db) -> None:
    buyer(db, "buy_9001", ts(2000))
    full_chain(db, "01", "buy_9001", ts(1900))
    coupon(db, "cpn_a", "buy_9001", ts(1900))
    report = purge_expired(db, NOW, apply=True, config=CFG)  # 외래키 순서가 틀리면 예외
    assert report.counts == {
        "refunds": 1,
        "returns": 1,
        "payments": 1,
        "order_items": 1,
        "orders": 1,
        "coupons": 1,
        "outbox_events": 0,
        "buyers": 1,
    }
    for table in ("buyers", "orders", "order_items", "payments", "refunds", "returns", "coupons"):
        assert count(db, table) == 0


def test_young_payment_keeps_old_order(db: Db) -> None:
    buyer(db, "buy_9001", ts(2000))
    order(db, "ord_a", "buy_9001", ts(1826))
    payment(db, "pay_a", "ord_a", ts(1824))  # 결제는 아직 5년 안
    report = purge_expired(db, NOW, apply=True, config=CFG)
    assert report.counts["orders"] == 0 and count(db, "orders") == 1 and count(db, "buyers") == 1


# --- 시드 -------------------------------------------------------------------


def test_seed_buyers_and_their_rows_are_never_deleted(db: Db) -> None:
    seed(db)
    with db.tx() as c:  # 시드 구매자 행을 아주 오래된 것으로 만든다
        c.execute("UPDATE buyers SET created_at = ?", (ts(5000),))
    order(db, "ord_a", "buy_0001", ts(3000))
    coupon(db, "cpn_a", "buy_0002", ts(3000))
    buyer(db, "buy_9001", ts(5000))
    report = purge_expired(db, NOW, apply=True, config=CFG)
    assert report.counts["buyers"] == 1  # buy_9001만
    assert count(db, "orders") == 1 and count(db, "coupons") == 1
    with db.tx() as c:
        left = {r[0] for r in c.execute("SELECT buyer_id FROM buyers")}
    assert left == SEED_BUYER_IDS


# --- dry-run / apply / 집계 ---------------------------------------------------

TABLES = (
    "buyers orders order_items payments refunds returns coupons outbox_events retention_daily_stats"
).split()


def snapshot(db: Db) -> dict[str, list[tuple]]:
    with db.tx() as c:
        return {
            t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in TABLES
        }


def populate(db: Db) -> None:
    buyer(db, "buy_9001", ts(2000))
    full_chain(db, "01", "buy_9001", ts(1900))
    coupon(db, "cpn_a", "buy_9001", ts(1900))
    outbox_row(db, "evt_a", ts(200), True)


def test_dry_run_is_default_and_changes_nothing(db: Db) -> None:
    populate(db)
    before = snapshot(db)
    report = purge_expired(db, NOW, config=CFG)
    assert report.applied is False and report.total == 8 and report.counts["outbox_events"] == 1
    assert snapshot(db) == before
    assert snapshot(db)["retention_daily_stats"] == []


def test_apply_deletes_and_dry_run_counts_match_apply(db: Db) -> None:
    populate(db)
    planned = purge_expired(db, NOW, config=CFG)
    done = purge_expired(db, NOW, apply=True, config=CFG)
    assert done.applied is True and done.counts == planned.counts
    assert purge_expired(db, NOW, apply=True, config=CFG).total == 0


def test_daily_stats_have_counts_and_sums_but_no_identifiers(db: Db) -> None:
    buyer(db, "buy_9001", ts(2000))
    order(db, "ord_a", "buy_9001", ts(1900), amount=1000)
    order(db, "ord_b", "buy_9001", ts(1900), amount=2500)
    purge_expired(db, NOW, apply=True, config=CFG)
    with db.tx() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(retention_daily_stats)")]
        rows = {r["kind"]: dict(r) for r in c.execute("SELECT * FROM retention_daily_stats")}
    assert cols == ["stat_date", "kind", "rows_count", "amount_sum", "tenant_id"]
    assert rows["orders"]["rows_count"] == 2 and rows["orders"]["amount_sum"] == 3500
    assert rows["orders"]["stat_date"] == ts(1900)[:10]
    assert rows["order_items"]["amount_sum"] is None
    flat = " ".join(str(v) for row in rows.values() for v in row.values())
    assert "buy_" not in flat and "ord_" not in flat


def test_stats_accumulate_across_runs(db: Db) -> None:
    buyer(db, "buy_9001", ts(10))
    order(db, "ord_a", "buy_9001", ts(1900, seconds=5), amount=100)
    purge_expired(db, NOW, apply=True, config=CFG)
    order(db, "ord_b", "buy_9001", ts(1900, seconds=9), amount=200)
    purge_expired(db, NOW, apply=True, config=CFG)
    with db.tx() as c:
        row = c.execute(
            "SELECT rows_count, amount_sum FROM retention_daily_stats WHERE kind = 'orders'"
        ).fetchone()
    assert tuple(row) == (2, 300)


def test_stats_table_is_created_on_existing_db_file(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    conn.commit()
    conn.close()
    file_db = Db(path)  # retention_daily_stats가 없던 파일
    assert count(file_db, "retention_daily_stats") == 0


# --- 환경변수 ----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "1.5", "99999999"])
def test_bad_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    for name in (
        "SHOP_RETENTION_TRANSACTION_DAYS",
        "SHOP_RETENTION_OUTBOX_DAYS",
        "SHOP_RETENTION_BUYER_DAYS",
    ):
        monkeypatch.setenv(name, bad)
    assert RetentionConfig.from_env() == RetentionConfig(1825, 90, 90)


def test_valid_env_values_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOP_RETENTION_TRANSACTION_DAYS", "30")
    monkeypatch.setenv("SHOP_RETENTION_OUTBOX_DAYS", " 7 ")
    monkeypatch.setenv("SHOP_RETENTION_BUYER_DAYS", "14")
    assert RetentionConfig.from_env() == RetentionConfig(30, 7, 14)


# --- 백그라운드 ---------------------------------------------------------------


def test_background_skipped_for_memory_db(db: Db) -> None:
    async def go() -> None:
        assert retention.start_background(db) is None

    asyncio.run(go())


def test_background_skipped_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_db = Db(str(tmp_path / "s.db"))
    monkeypatch.setenv("SHOP_RETENTION_ENABLED", "0")

    async def go() -> None:
        assert retention.start_background(file_db) is None

    asyncio.run(go())


def test_background_runs_dry_run_by_default_on_file_db(tmp_path: Path) -> None:
    file_db = Db(str(tmp_path / "s.db"))
    buyer(file_db, "buy_9001", ts(5000))
    report = retention.run_once(file_db, NOW)
    assert report is not None and report.applied is False and report.counts["buyers"] == 1
    assert count(file_db, "buyers") == 1


def test_background_apply_requires_env_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_db = Db(str(tmp_path / "s.db"))
    buyer(file_db, "buy_9001", ts(5000))
    monkeypatch.setenv("SHOP_RETENTION_APPLY", "true")
    first = retention.run_once(file_db, NOW)
    assert first is not None and first.applied is False
    monkeypatch.setenv("SHOP_RETENTION_APPLY", "1")
    second = retention.run_once(file_db, NOW)
    assert second is not None and second.applied is True
    assert count(file_db, "buyers") == 0


def test_background_task_starts_on_file_db_and_app_still_boots(tmp_path: Path) -> None:
    file_db = Db(str(tmp_path / "s.db"))

    async def go() -> None:
        task = retention.start_background(file_db, interval=3600)
        assert task is not None
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(go())
    assert create_app(file_db) is not None


def test_background_failure_is_swallowed_and_logs_only_the_exception_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    file_db = Db(str(tmp_path / "s.db"))

    def boom(*_a, **_k):
        raise RuntimeError("buy_9001 1234원")

    monkeypatch.setattr(retention, "purge_expired", boom)
    with caplog.at_level(logging.INFO, logger="ax_shop_api.retention"):
        assert retention.run_once(file_db, NOW) is None
    assert "RuntimeError" in caplog.text
    assert "buy_9001" not in caplog.text and "1234" not in caplog.text


def test_report_and_logs_contain_no_buyer_id_or_amount(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    file_db = Db(str(tmp_path / "s.db"))
    buyer(file_db, "buy_9001", ts(5000))
    order(file_db, "ord_a", "buy_9001", ts(2000), amount=987654)
    with caplog.at_level(logging.INFO, logger="ax_shop_api.retention"):
        report = retention.run_once(file_db, NOW)
    assert report is not None
    text = report.summary() + caplog.text + repr(report)
    assert "buy_9001" not in text and "987654" not in text and "ord_a" not in text


# --- purge-buyer --------------------------------------------------------------


def make_target(db: Db) -> None:
    buyer(db, "buy_9001", ts(10))
    buyer(db, "buy_9002", ts(10))
    full_chain(db, "01", "buy_9001", ts(30))
    coupon(db, "cpn_a", "buy_9001", ts(30))
    full_chain(db, "02", "buy_9002", ts(30))
    outbox_row(db, "evt_c", ts(1), True, '{"data": {"order_id": "ord_01", "buyer_id": "buy_9001"}}')
    outbox_row(db, "evt_d", ts(1), True, '{"data": {"order_id": "ord_01"}}')
    outbox_row(db, "evt_e", ts(1), False, '{"data": {"order_id": "ord_01"}}')  # 미발송 보존
    outbox_row(db, "evt_f", ts(1), True, '{"data": {"order_id": "ord_02"}}')  # 다른 구매자


def test_purge_buyer_default_dry_run_changes_nothing(db: Db) -> None:
    make_target(db)
    before = snapshot(db)
    report = purge_buyer(db, "buy_9001")
    assert report.applied is False and report.counts["outbox_events"] == 2
    assert snapshot(db) == before


def test_purge_buyer_apply_keeps_retained_transaction_records(db: Db) -> None:
    make_target(db)
    report = purge_buyer(db, "buy_9001", apply=True)
    assert report.counts["outbox_events"] == 2 and report.counts["buyers"] == 0
    assert count(db, "orders") == 2 and count(db, "payments") == 2 and count(db, "coupons") == 1
    with db.tx() as c:
        left = {r[0] for r in c.execute("SELECT event_id FROM outbox_events")}
    assert left == {"evt_e", "evt_f"}
    assert count(db, "buyers") == 2


def test_purge_buyer_default_removes_buyer_row_when_nothing_retained(db: Db) -> None:
    buyer(db, "buy_9001", ts(1))
    report = purge_buyer(db, "buy_9001", apply=True)
    assert report.counts["buyers"] == 1 and count(db, "buyers") == 0


def test_purge_buyer_include_retained_removes_everything_of_that_buyer_only(db: Db) -> None:
    make_target(db)
    report = purge_buyer(db, "buy_9001", apply=True, include_retained=True)
    assert report.counts == {
        "refunds": 1,
        "returns": 1,
        "payments": 1,
        "order_items": 1,
        "orders": 1,
        "coupons": 1,
        "outbox_events": 2,
        "buyers": 1,
    }
    with db.tx() as c:
        assert [r[0] for r in c.execute("SELECT buyer_id FROM buyers")] == ["buy_9002"]
        assert [r[0] for r in c.execute("SELECT order_id FROM orders")] == ["ord_02"]
    assert count(db, "retention_daily_stats") > 0


def test_purge_buyer_include_retained_dry_run_changes_nothing(db: Db) -> None:
    make_target(db)
    before = snapshot(db)
    report = purge_buyer(db, "buy_9001", include_retained=True)
    assert report.total == 9 and snapshot(db) == before


def test_purge_buyer_rejects_seed_and_unknown(db: Db) -> None:
    seed(db)
    for seed_id in ("buy_0001", sorted(SEED_BUYER_IDS)[-1]):
        with pytest.raises(SeedBuyerError):
            purge_buyer(db, seed_id, apply=True, include_retained=True)
    assert count(db, "buyers") == len(SEED_BUYER_IDS)
    with pytest.raises(BuyerNotFoundError):
        purge_buyer(db, "buy_7777")


# --- CLI ----------------------------------------------------------------------


def test_cli_run_and_purge_buyer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = str(tmp_path / "cli.db")
    file_db = Db(path)
    buyer(file_db, "buy_9001", "2000-01-01T00:00:00Z")
    buyer(file_db, "buy_9002", "2000-01-01T00:00:00Z")
    order(file_db, "ord_a", "buy_9002", "2000-01-01T00:00:00Z", amount=424242)
    monkeypatch.setenv("SHOP_DB_PATH", path)

    assert retention.main(["run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "buy_" not in out and "424242" not in out
    assert count(file_db, "buyers") == 2

    assert retention.main(["purge-buyer", "buy_9002"]) == 0
    assert count(file_db, "orders") == 1
    assert retention.main(["purge-buyer", "buy_9002", "--apply", "--include-retained"]) == 0
    out = capsys.readouterr().out
    assert "buy_9002" not in out and "424242" not in out
    assert count(file_db, "orders") == 0

    assert retention.main(["run", "--apply"]) == 0
    assert count(file_db, "buyers") == 0

    assert retention.main(["purge-buyer", "buy_0001", "--apply"]) == 2
    assert retention.main(["purge-buyer", "buy_7777"]) == 1


def test_cli_refuses_memory_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SHOP_DB_PATH", ":memory:")
    assert retention.main(["run", "--apply"]) == 2
    assert capsys.readouterr().out == ""
