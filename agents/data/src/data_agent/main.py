"""ax-agent-data 서버 진입점."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from data_agent.handlers import HANDLERS


def _card_path() -> Path:
    """`agents/data/agent-card.json`을 찾는다. 컨테이너와 레포 루트 실행 모두에서 동작한다."""
    override = os.getenv("DATA_AGENT_CARD")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "agents" / "data" / "agent-card.json",
        Path(__file__).resolve().parents[2] / "agent-card.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise FileNotFoundError("agent-card.json을 찾을 수 없다 (DATA_AGENT_CARD로 경로를 지정한다)")


def load_card() -> AgentCard:
    return AgentCard.model_validate_json(_card_path().read_text(encoding="utf-8"))


def build_app() -> FastAPI:
    """카드로 A2A 앱을 만든다. 카드와 핸들러의 skill이 다르면 시작할 때 실패한다."""
    return create_agent_app(load_card(), HANDLERS)


# /health, /.well-known/agent-card.json, /a2a 는 템플릿이 만든다.
app = build_app()
