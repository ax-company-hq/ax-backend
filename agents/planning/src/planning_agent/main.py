"""ax-agent-planning 서버 진입점.

- 템플릿 `create_agent_app(card, HANDLERS)`가 `/.well-known/agent-card.json`, `/health`,
  `POST /a2a`를 준다.
- 화면용 REST·SSE(`/api/planning/*`)를 같은 앱에 붙인다.
  A2A 핸들러와 REST는 같은 상태 저장소를 본다.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.middleware.cors import CORSMiddleware

from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from planning_agent.api import create_api_router
from planning_agent.handlers import HANDLERS
from planning_agent.runtime import get_service

# 화면: 업무 시스템 dashboard-web 「기획개발 AI」 탭(3001), 옛 planning-web(3002·5173)
DEFAULT_CORS_ORIGINS = (
    "http://localhost:3001,http://127.0.0.1:3001,"
    "http://localhost:3002,http://localhost:5173,http://127.0.0.1:5173"
)


def _card_path() -> Path:
    """`agents/planning/agent-card.json`을 찾는다. 컨테이너와 레포 루트 실행 모두에서 동작한다."""
    override = os.getenv("PLANNING_AGENT_CARD")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "agents" / "planning" / "agent-card.json",
        Path(__file__).resolve().parents[2] / "agent-card.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise FileNotFoundError(
        "agent-card.json을 찾을 수 없다 (PLANNING_AGENT_CARD로 경로를 지정한다)"
    )


def load_card() -> AgentCard:
    return AgentCard.model_validate_json(_card_path().read_text(encoding="utf-8"))


def _cors_origins() -> list[str]:
    raw = os.getenv("PLANNING_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [o.strip() for o in raw.split(",") if o.strip()]


app = create_agent_app(load_card(), HANDLERS)
app.include_router(create_api_router(get_service))
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
