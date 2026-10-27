"""매일 결산 자동 실행(①): 주기 설정, 전날 기준 계산, 저장·조회, 루프. 네트워크 없음."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from finance_agent import handlers, main
from finance_agent.main import app
from finance_agent.sources import MockSources

YESTERDAY = date(2026, 10, 22)  # conftest의 FIXED_NOW(2026-10-23) 전날


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch, fixed_now) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    events.reset_mock_bus()
    handlers.reset_state(MockSources())


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 86400), ("", 86400), ("0", 0), ("60", 60), ("-5", 0), ("x", 86400)]
)
def test_daily_closing_interval_env(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int
) -> None:
    if raw is None:
        monkeypatch.delenv("FINANCE_DAILY_CLOSING_SECONDS", raising=False)
    else:
        monkeypatch.setenv("FINANCE_DAILY_CLOSING_SECONDS", raw)
    assert main.daily_closing_seconds() == expected


@pytest.mark.asyncio
async def test_run_stores_yesterday_report(kit) -> None:
    """전날 하루를 기준으로 계산하고, 결과를 state에 남긴다. 확정은 하지 않는다."""
    orders = [kit.order("ord_1", kit.at("2026-10-22"), [("sel_a", "books", 2, 10000)])]
    handlers.state().books_loader = lambda: _books(kit, orders)

    stored = await main.run_and_store_daily_closing("auto")

    assert stored["date"] == YESTERDAY.isoformat()
    assert stored["trigger"] == "auto"
    datetime.fromisoformat(stored["ran_at"])
    gmv = next(x["amount"] for x in stored["report"]["lines"] if x["code"] == "gmv")
    assert gmv == 20000 and stored["report"]["status"] == "draft"
    assert handlers.state().last_daily_closing == stored
    # 자동 결산은 조회만 한다: 확정 기록이 생기면 안 된다
    assert handlers.state().records.closings == {}


@pytest.mark.asyncio
async def test_run_keeps_existing_confirmation(kit) -> None:
    """사람이 이미 확정한 날이면 그 확정 기록을 그대로 붙여서 보여 준다."""
    orders = [kit.order("ord_1", kit.at("2026-10-22"), [("sel_a", "books", 1, 5000)])]
    handlers.state().books_loader = lambda: _books(kit, orders)
    handlers.state().records.closings[("day", YESTERDAY.isoformat())] = {
        "by": "태윤",
        "at": "2026-10-23T00:10:00+00:00",
    }

    stored = await main.run_and_store_daily_closing("auto")

    assert stored["report"]["status"] == "confirmed"
    assert stored["report"]["confirmed_by"] == "태윤"


def test_last_endpoint_returns_stored_result(kit) -> None:
    """`GET /api/finance/closing/last`는 저장된 결과와 주기 정보를 돌려준다."""
    client = TestClient(app)
    body = client.get("/api/finance/closing/last").json()
    assert body == {"data": None, "meta": {"interval_seconds": 0, "next_run_at": None}}

    handlers.state().last_daily_closing = {"date": YESTERDAY.isoformat(), "trigger": "auto"}
    main.DAILY_CLOSING_SCHEDULE.update(interval_seconds=86400, next_run_at="2026-10-24T00:00:00Z")
    try:
        body = client.get("/api/finance/closing/last").json()
        assert body["data"]["date"] == YESTERDAY.isoformat()
        assert body["meta"]["interval_seconds"] == 86400
        assert body["meta"]["next_run_at"] == "2026-10-24T00:00:00Z"
    finally:
        main.DAILY_CLOSING_SCHEDULE.update(interval_seconds=0, next_run_at=None)


@pytest.mark.asyncio
async def test_loop_runs_repeatedly_and_survives_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """한 번 실패해도 루프는 계속 돌고, 다음 실행 시각을 남긴다 (실제 시간을 기다리지 않음)."""
    calls: list[int] = []
    done = asyncio.Event()

    async def flaky(trigger: str) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) >= 3:
            done.set()
        return {"date": YESTERDAY.isoformat(), "trigger": trigger}

    monkeypatch.setattr(main, "run_and_store_daily_closing", flaky)
    task = asyncio.create_task(main.daily_closing_loop(0, first_delay=0))
    await asyncio.wait_for(done.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 3
    assert main.DAILY_CLOSING_SCHEDULE["next_run_at"] is not None


def test_lifespan_starts_daily_closing_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    async def fake_run(trigger: str) -> dict[str, Any]:
        stored = {"date": YESTERDAY.isoformat(), "trigger": trigger}
        handlers.state().last_daily_closing = stored
        return stored

    monkeypatch.setattr(main, "run_and_store_daily_closing", fake_run)
    monkeypatch.setattr(main, "DAILY_CLOSING_FIRST_DELAY", 0)
    monkeypatch.setenv("FINANCE_DAILY_CLOSING_SECONDS", "86400")
    with TestClient(app) as client:
        body: dict = {}
        for _ in range(200):
            body = client.get("/api/finance/closing/last").json()
            if body["data"]:
                break
            time.sleep(0.01)
        assert body["data"]["trigger"] == "auto"
        assert body["meta"]["interval_seconds"] == 86400
        assert datetime.fromisoformat(body["meta"]["next_run_at"]).tzinfo is not None
    assert main.DAILY_CLOSING_SCHEDULE == {"interval_seconds": 0, "next_run_at": None}


def test_lifespan_does_not_start_daily_closing_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(trigger: str) -> dict[str, Any]:
        raise AssertionError("자동 결산이 꺼져 있어야 한다")

    monkeypatch.setattr(main, "run_and_store_daily_closing", fake_run)
    monkeypatch.setattr(main, "DAILY_CLOSING_FIRST_DELAY", 0)
    monkeypatch.setenv("FINANCE_DAILY_CLOSING_SECONDS", "0")
    with TestClient(app) as client:
        meta = client.get("/api/finance/closing/last").json()["meta"]
        assert (meta["interval_seconds"], meta["next_run_at"]) == (0, None)
        assert handlers.state().last_daily_closing is None


async def _books(kit, orders: list[dict[str, Any]]):
    """`books_loader`가 돌려줄 코루틴."""
    return kit.books(orders=orders, refunds=[], coupons=[])
