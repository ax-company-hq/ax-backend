"""ax-agent-finance 서버 진입점."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ax_agent_template.events import subscribe
from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from finance_agent.handlers import HANDLERS, SUBSCRIBED_EVENTS, on_event

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


app = create_agent_app(load_card(), HANDLERS)
subscribe_events()
