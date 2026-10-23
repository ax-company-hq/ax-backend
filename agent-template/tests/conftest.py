"""테스트 공용 설정: 목 모드 고정, 장난감 에이전트, 요청 만들기."""

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI

from ax_agent_template import server
from ax_agent_template.server import create_agent_app
from ax_agent_template.testing import reset_state  # noqa: F401  (autouse 픽스처)
from ax_contracts.schemas import AgentCard, AgentRequest, AgentResult, TaskStatus

CARD = AgentCard.model_validate(
    {
        "name": "echo_agent",
        "owner": "test",
        "description": "테스트용 에이전트",
        "url": "http://echo_agent:9999/a2a",
        "skills": [
            {
                "id": skill_id,
                "description": f"{skill_id} 테스트 skill. 상태를 바꾸지 않는다.",
                "input_schema": "x",
                "output_schema": "x",
                "grade": "read",
                "allowed_requesters": ["orchestrator"],
                "priority": "optional",
            }
            for skill_id in ("echo", "boom", "broken_output")
        ],
    }
)


async def _echo(request: AgentRequest) -> AgentResult:
    return AgentResult(
        meta=request.meta,
        status=TaskStatus.COMPLETED,
        output={"input": request.input, "path": request.meta.path},
    )


async def _boom(request: AgentRequest) -> AgentResult:
    raise RuntimeError("의도한 오류")


async def _broken_output(request: AgentRequest) -> dict[str, Any]:
    return {"status": "not-a-status"}


def _request_body(
    skill: str = "echo",
    *,
    token: str = "mock:orchestrator",
    path: list[str] | None = None,
    deadline_in: float = 60.0,
    **meta_extra: Any,
) -> dict[str, Any]:
    deadline = datetime.now(UTC) + timedelta(seconds=deadline_in)
    return {
        "meta": {
            "context_id": "ctx_req_test",
            "token": token,
            "path": path or [],
            "deadline": deadline.isoformat(),
            **meta_extra,
        },
        "skill": skill,
        "input": {"n": 1},
    }


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch: pytest.MonkeyPatch):
    """모든 테스트는 목 모드로 돈다. 전역 상태는 테스트마다 되돌린다."""
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    server.MOCK_ESCALATIONS.clear()
    yield


@pytest.fixture
def card() -> AgentCard:
    return CARD


@pytest.fixture
def handlers() -> dict[str, Callable[[AgentRequest], Awaitable[Any]]]:
    return {"echo": _echo, "boom": _boom, "broken_output": _broken_output}


@pytest.fixture
def app(card: AgentCard, handlers: dict) -> FastAPI:
    return create_agent_app(card, handlers)


@pytest.fixture
def request_body() -> Callable[..., dict[str, Any]]:
    return _request_body


@pytest.fixture
def real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """목 모드를 모두 끈다. CI는 `<부서>_MOCK=1`도 켜 두므로 그것까지 지운다."""
    for key in list(os.environ):
        if key == "AGENT_TEMPLATE_MOCK" or key.endswith("_MOCK"):
            monkeypatch.delenv(key)
