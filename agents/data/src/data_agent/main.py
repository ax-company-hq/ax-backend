"""ax-agent-data 서버 진입점."""

from __future__ import annotations

import json
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
    """카드로 A2A 앱을 만든다.

    TODO: 채워진 카드(PR #70)가 머지되면 빈 카드 처리(아래 `health_only`)를 지운다.
    카드 파일이 아직 빈 자리표시자(skills가 비어 있음)면 /health만 있는 앱으로 뜬다.
    """
    raw = json.loads(_card_path().read_text(encoding="utf-8"))
    if not raw.get("skills"):
        health_only = FastAPI(title="ax-agent-data")

        @health_only.get("/health")
        def health() -> dict:
            return {"data": {"status": "ok"}, "meta": {}}

        return health_only
    card = load_card()
    return create_agent_app(card, HANDLERS)


# /health는 템플릿의 create_agent_app이 같은 응답으로 제공한다.
app = build_app()
