"""planning 테스트 공용 설정: 목 모드 고정, 테스트마다 새 상태 저장소, A2A 요청 만들기."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ax_agent_template import config
from planning_agent.domain.service import PlanningService
from planning_agent.runtime import set_service


@pytest.fixture(autouse=True)
def service(monkeypatch: pytest.MonkeyPatch) -> PlanningService:
    """모든 테스트는 목 모드(`AGENT_TEMPLATE_MOCK=1`)로 돌고, 초기 데이터에서 시작한다."""
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    saved = config._agent_name
    config.set_agent_name("planning")  # 다른 부서 테스트가 먼저 돌아도 execute 요청자는 planning
    svc = set_service(PlanningService())
    yield svc
    config._agent_name = saved


def _a2a_body(
    skill: str,
    payload: dict[str, Any],
    *,
    token: str = "mock:infra",
    path: list[str] | None = None,
    context_id: str = "ctx_inc_0001",
    idempotency_key: str | None = None,
    untrusted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "meta": {
            "context_id": context_id,
            "token": token,
            "path": path or [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": idempotency_key,
        },
        "skill": skill,
        "input": payload,
        "untrusted": untrusted or {},
    }


@pytest.fixture
def a2a_body():
    """A2A 요청 본문을 만드는 함수 (기본 요청자 infra, 사건 ctx_inc_0001)."""
    return _a2a_body
