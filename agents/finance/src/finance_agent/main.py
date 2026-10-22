"""ax-agent-finance 서버 진입점."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ax_agent_template.events import subscribe
from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from finance_agent.handlers import HANDLERS, SUBSCRIBED_EVENTS, on_event, state

# 대시보드(dashboard-web)는 조회(GET)만 한다. A2A(`POST /a2a`)는 에이전트끼리만 쓴다
DEFAULT_CORS_ORIGINS = "http://localhost:3001,http://127.0.0.1:3001"

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


def add_read_api(app: FastAPI) -> None:
    """대시보드용 조회 API. 상태를 바꾸지 않는다. 응답은 `{data, meta}`."""

    @app.get("/api/finance/verifications")
    def verifications(context_id: str | None = None) -> dict:
        rows = state().ledger.verify_log
        if context_id:
            rows = [r for r in rows if r["context_id"] == context_id]
        return {"data": list(reversed(rows)), "meta": {"count": len(rows)}}

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

    @app.get("/api/finance/market-ops")
    def market_ops() -> dict:
        rows = state().ledger.market_ops
        return {"data": list(reversed(rows)), "meta": {"count": len(rows)}}


def create_app() -> FastAPI:
    app = create_agent_app(load_card(), HANDLERS)
    origins = os.getenv("FINANCE_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins.split(",") if o.strip()],
        allow_methods=["GET"],
    )
    add_read_api(app)
    return app


app = create_app()
subscribe_events()
