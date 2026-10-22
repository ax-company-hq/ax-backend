"""ax-agent-marketing 서버 진입점."""

from __future__ import annotations

import os
from pathlib import Path

from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from marketing_agent.handlers import HANDLERS


def _card_path() -> Path:
    """`agents/marketing/agent-card.json`을 찾는다. 컨테이너와 레포 루트 실행 모두에서 동작한다."""
    override = os.getenv("MARKETING_AGENT_CARD")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "agents" / "marketing" / "agent-card.json",
        Path(__file__).resolve().parents[2] / "agent-card.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise FileNotFoundError(
        "agent-card.json을 찾을 수 없다 (MARKETING_AGENT_CARD로 경로를 지정한다)"
    )


def load_card() -> AgentCard:
    return AgentCard.model_validate_json(_card_path().read_text(encoding="utf-8"))


app = create_agent_app(load_card(), HANDLERS)
