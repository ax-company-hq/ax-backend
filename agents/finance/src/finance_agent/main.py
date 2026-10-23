"""ax-agent-finance 서버 진입점."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from ax_agent_template.events import subscribe
from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from finance_agent import catbooks
from finance_agent.finance_api import add_finance_api
from finance_agent.handlers import HANDLERS, SUBSCRIBED_EVENTS, on_event, recompute, state
from finance_agent.selftest import SELF_TEST_TTL_HOURS, is_fresh, run_self_test

# 대시보드(dashboard-web)는 브라우저에서 이 서버를 바로 부른다. 대부분 조회(GET)이고, 사람 결정
# 경로(`DECISION_PATHS`)만 POST·Authorization을 허용한다. A2A(`POST /a2a`)는 에이전트끼리만 쓴다
DEFAULT_CORS_ORIGINS = "http://localhost:3001,http://127.0.0.1:3001"
DECISION_PATHS = re.compile(
    r"^/api/finance/(closing/confirm|reconciliation/[^/]+/note|settlements/batch)$"
)

logger = logging.getLogger("ax.finance")


def _card_path() -> Path:
    """`agents/finance/agent-card.json`을 찾는다. 컨테이너와 레포 루트 실행 모두에서 동작한다."""
    override = os.getenv("FINANCE_AGENT_CARD")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "agents" / "finance" / "agent-card.json",
        Path(__file__).resolve().parents[2] / "agent-card.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise FileNotFoundError("agent-card.json을 찾을 수 없다 (FINANCE_AGENT_CARD로 경로를 지정한다)")


def load_card() -> AgentCard:
    return AgentCard.model_validate_json(_card_path().read_text(encoding="utf-8"))


def subscribe_events() -> bool:
    """지급·환불 이벤트를 구독한다. 이벤트 버스가 아직 없으면(템플릿 TODO) 건너뛴다."""
    try:
        subscribe(SUBSCRIBED_EVENTS, on_event)
    except NotImplementedError:
        logger.warning("이벤트 버스 미연결: coupon.issued·payment.refunded를 받지 못한다")
        return False
    return True


class DashboardCORS:
    """출처는 `FINANCE_CORS_ORIGINS`. 사람 결정 경로만 POST를 열고, 나머지는 GET만 연다."""

    def __init__(self, app: ASGIApp, origins: list[str]) -> None:
        self.get_only = CORSMiddleware(app, allow_origins=origins, allow_methods=["GET"])
        self.decisions = CORSMiddleware(
            app,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        target = self.decisions if DECISION_PATHS.match(path) else self.get_only
        await target(scope, receive, send)


def cors_origins() -> list[str]:
    raw = os.getenv("FINANCE_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip()]


def add_read_api(app: FastAPI) -> None:
    """대시보드용 기존 조회 API. 상태를 바꾸지 않는다. 응답은 `{data, meta}`.

    재무 재설계 API(overview·closing·reconciliation·settlements·policy·audit)는 `finance_api.py`.
    """

    @app.get("/api/finance/ledger")
    def ledger() -> dict:
        book = state().ledger
        compensation = [{"context_id": cid, **row} for cid, row in book.coupons.items()]
        refunds = [{"order_id": oid, "refunded": amount} for oid, amount in book.refunds.items()]
        return {"data": {"compensation": compensation, "refunds": refunds}, "meta": {}}

    @app.get("/api/finance/loss-reports")
    def loss_reports() -> dict:
        rows = sorted(state().ledger.loss_reports.values(), key=lambda r: r["at"], reverse=True)
        return {"data": rows, "meta": {"count": len(rows)}}

    @app.get("/api/finance/self-test")
    async def self_test() -> dict:
        """재무 AI 테스트 버튼: 가짜 자료로 판단 점검, 연결 확인. 상태를 바꾸지 않는다."""
        result = await run_self_test(recompute, state().policy)
        state().last_self_test = result
        return {"data": result, "meta": {}}

    @app.get("/api/finance/self-test/last")
    def last_self_test() -> dict:
        """마지막 테스트 결과. 12시간이 지나면 지운다(아침마다 새로 테스트하도록)."""
        last = state().last_self_test
        if last and not is_fresh(last):
            state().last_self_test = last = None
        return {"data": last, "meta": {"ttl_hours": SELF_TEST_TTL_HOURS}}

    @app.get("/api/finance/catbooks/sync")
    async def catbooks_sync() -> dict:
        """캣북스에서 지금 가져와 확인한다. 캣북스는 읽기만 하고 재무 기록만 갱신한다."""
        summary = await catbooks.sync()
        return {"data": summary, "meta": {"poll_seconds": catbooks.poll_seconds()}}

    @app.get("/api/finance/catbooks/items")
    def catbooks_items() -> dict:
        """캣북스에서 들어온 처리할 일. 최근 신청 먼저."""
        rows = catbooks.items_newest_first()
        return {
            "data": rows,
            "meta": {
                "count": len(rows),
                "sync": state().ledger.catbooks_sync,
                "poll_seconds": catbooks.poll_seconds(),
            },
        }

    @app.get("/api/finance/threshold-reviews")
    def threshold_reviews() -> dict:
        rows = state().ledger.threshold_reviews
        return {"data": list(reversed(rows)), "meta": {"count": len(rows)}}

    @app.get("/api/finance/market-ops")
    def market_ops() -> dict:
        rows = state().ledger.market_ops
        return {"data": list(reversed(rows)), "meta": {"count": len(rows)}}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """서버가 도는 동안 캣북스를 주기적으로 가져온다 (`FINANCE_CATBOOKS_POLL_SECONDS=0`이면 끔)."""
    interval = catbooks.poll_seconds()
    task = asyncio.create_task(catbooks.poll_forever(interval)) if interval else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = create_agent_app(load_card(), HANDLERS)
    app.router.lifespan_context = lifespan
    app.add_middleware(DashboardCORS, origins=cors_origins())
    add_read_api(app)
    add_finance_api(app)
    return app


app = create_app()
subscribe_events()
