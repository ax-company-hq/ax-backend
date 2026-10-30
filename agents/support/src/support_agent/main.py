"""ax-agent-support 서버 진입점."""

from __future__ import annotations

import os
from pathlib import Path

from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from support_agent import receipt_vision
from support_agent.change_handlers import CHANGE_HANDLERS
from support_agent.claim_analysis import ANALYSIS_HANDLERS
from support_agent.claim_handlers import CLAIM_HANDLERS
from support_agent.gap_handlers import GAP_HANDLERS
from support_agent.handlers import HANDLERS
from support_agent.handoff_handlers import HANDOFF_HANDLERS
from support_agent.incident_handlers import INCIDENT_HANDLERS
from support_agent.insight_handlers import INSIGHT_HANDLERS
from support_agent.payment_handlers import PAYMENT_HANDLERS
from support_agent.privacy_handlers import PRIVACY_HANDLERS
from support_agent.signal_handlers import SIGNAL_HANDLERS
from support_agent.suggest_handlers import SUGGEST_HANDLERS


def _card_path() -> Path:
    """`agents/support/agent-card.json`을 찾는다. 컨테이너와 레포 루트 실행 모두에서 동작한다."""
    override = os.getenv("SUPPORT_AGENT_CARD")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "agents" / "support" / "agent-card.json",
        Path(__file__).resolve().parents[2] / "agent-card.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise FileNotFoundError("agent-card.json을 찾을 수 없다 (SUPPORT_AGENT_CARD로 경로를 지정한다)")


def load_card() -> AgentCard:
    return AgentCard.model_validate_json(_card_path().read_text(encoding="utf-8"))


ALL_HANDLERS = {
    **HANDLERS,
    **CLAIM_HANDLERS,
    **INSIGHT_HANDLERS,
    **CHANGE_HANDLERS,
    **ANALYSIS_HANDLERS,
    **SUGGEST_HANDLERS,
    **INCIDENT_HANDLERS,
    **PAYMENT_HANDLERS,
    **HANDOFF_HANDLERS,
    **GAP_HANDLERS,
    **PRIVACY_HANDLERS,
    **SIGNAL_HANDLERS,
}
# 영수증 사진 분석기: 이미지를 읽을 모델이 설정돼 있을 때만 꽂는다(목 모드·미설정이면 꽂지 않는다)
receipt_vision.connect()
app = create_agent_app(load_card(), ALL_HANDLERS)
