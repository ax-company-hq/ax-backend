"""총괄 자신의 `/a2a` skill (ORC-01, #272).

지금은 `find_active_incident` 하나다. 고객지원이 문의를 접수할 때 진행 중인 사건 번호
(`ctx_inc_`)를 이어받게 하려고 부른다(#274). 읽기 전용이라 사건을 열거나 바꾸지 않는다.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from pydantic import ValidationError

from ax_agent_template.server import Handler, create_agent_app
from ax_contracts.schemas import (
    AgentCard,
    AgentRequest,
    AgentResult,
    Evidence,
    FindActiveIncidentInput,
    FindActiveIncidentOutput,
    TaskStatus,
)
from ax_orchestrator.manager import IncidentManager

GetManager = Callable[[], IncidentManager | None]


def _card_path() -> Path:
    """`orchestrator/agent-card.json`을 찾는다. 컨테이너와 레포 루트 실행 모두에서 동작한다."""
    override = os.getenv("ORCHESTRATOR_AGENT_CARD")
    candidates = [
        Path(override) if override else None,
        Path.cwd() / "orchestrator" / "agent-card.json",
        Path(__file__).resolve().parents[2] / "agent-card.json",
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    raise FileNotFoundError(
        "agent-card.json을 찾을 수 없다 (ORCHESTRATOR_AGENT_CARD로 경로를 지정한다)"
    )


def load_card() -> AgentCard:
    return AgentCard.model_validate_json(_card_path().read_text(encoding="utf-8"))


def find_active_incident(get_manager: GetManager) -> Handler:
    async def handler(request: AgentRequest) -> AgentResult:
        try:
            parsed = FindActiveIncidentInput.model_validate(request.input)
        except ValidationError as error:
            errors = [f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in error.errors()]
            return AgentResult(
                meta=request.meta,
                status=TaskStatus.FAILED,
                outcome="invalid_input",
                output={"errors": errors},
            )
        manager = get_manager()
        inc = manager.find_active(parsed.component, parsed.occurred_at) if manager else None
        if inc is None:
            output = FindActiveIncidentOutput(found=False)
            summary = "맞는 사건 없음"
        else:
            output = FindActiveIncidentOutput(
                found=True,
                context_id=inc.context_id,
                component=inc.component,
                status=inc.status,  # type: ignore[arg-type]
                opened_at=inc.opened_at,
                closed_at=inc.closed_at,
                notice_id=inc.shared.get("notice_id"),  # 점검 공지가 게시됐을 때만, 지어내지 않는다
            )
            summary = f"{inc.component} 사건 {inc.status}"
        return AgentResult(
            meta=request.meta,
            status=TaskStatus.COMPLETED,
            outcome="found" if output.found else "not_found",
            output=output.model_dump(mode="json"),
            evidence=Evidence(
                queries=["find_active_incident"],
                data_summary=summary,
                collected_at=datetime.now(UTC),
                ref_ids=[output.context_id] if output.context_id else [],
            ),
        )

    return handler


def build_a2a_app(get_manager: GetManager) -> FastAPI:
    """`/a2a`와 `/.well-known/agent-card.json`을 가진 앱. 총괄 서버 맨 끝에 붙인다."""
    return create_agent_app(
        load_card(), {"find_active_incident": find_active_incident(get_manager)}
    )
