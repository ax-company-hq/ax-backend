"""ax-agent-hr 서버 진입점."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from ax_agent_template.server import create_agent_app
from ax_contracts.schemas import AgentCard
from hr_agent.config import HrSettings
from hr_agent.handlers import HANDLERS


def _card_path() -> Path:
    """`agents/hr/agent-card.json`을 찾는다. 컨테이너와 레포 루트 실행 모두에서 동작한다."""
    override = os.getenv("HR_AGENT_CARD")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "agents" / "hr" / "agent-card.json",
        Path(__file__).resolve().parents[2] / "agent-card.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise FileNotFoundError("agent-card.json을 찾을 수 없다 (HR_AGENT_CARD로 경로를 지정한다)")


def load_card() -> AgentCard:
    return AgentCard.model_validate_json(_card_path().read_text(encoding="utf-8"))


app = create_agent_app(load_card(), HANDLERS)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """`HR_TIMER=1`이면 교대 시각 점검(곧 끝나는 교대의 인수인계 자동 시작)을 주기로 돌린다."""
    task = None
    if HrSettings.from_env().timer_enabled:
        from hr_agent.roster.timer import run_forever

        task = asyncio.create_task(run_forever())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app.router.lifespan_context = _lifespan
