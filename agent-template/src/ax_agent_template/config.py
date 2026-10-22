"""템플릿 공통 설정: 에이전트 이름·토큰·주소·목 모드.

환경변수는 부서 접두사 대문자(`HR_MOCK`, `HR_TOKEN`)이고, 공통 값은 접두사 없이 쓴다.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# CLAUDE.md 포트 표. 컨테이너에서는 서비스 이름이 호스트 이름이다.
DEFAULT_PORTS: dict[str, int] = {
    "orchestrator": 8000,
    "safety": 8001,
    "memory": 8002,
    "shop-api": 8010,
    "dashboard-api": 8020,
    "planning": 8101,
    "support": 8102,
    "marketing": 8103,
    "hr": 8104,
    "finance": 8105,
    "data": 8106,
    "infra": 8107,
}

# 이 길이를 넘는 path는 이관 큐로 넘긴다.
MAX_PATH_LENGTH = 3

# 프로세스 기본 이름(마지막 `set_agent_name`). 요청 처리 중에는 `_request_agent_name`이 우선한다.
_agent_name: str | None = None
# 요청을 받은 앱의 이름. 앱을 여러 개 만들어도 서로 섞이지 않게 요청 단위로 격리한다.
_request_agent_name: ContextVar[str | None] = ContextVar("ax_request_agent_name", default=None)


def env_prefix(name: str) -> str:
    """에이전트·서비스 이름을 환경변수 접두사로 바꾼다 (`shop-api` → `SHOP_API`)."""
    return name.upper().replace("-", "_")


def set_agent_name(name: str) -> None:
    """`create_agent_app`이 호출한다. 이후 `call_agent`·`execute`가 이 이름을 쓴다."""
    global _agent_name
    _agent_name = name


@contextmanager
def agent_name_scope(name: str) -> Iterator[None]:
    """이 범위 안에서는 전역 이름 대신 `name`을 쓴다 (앱의 요청 처리용)."""
    token = _request_agent_name.set(name)
    try:
        yield
    finally:
        _request_agent_name.reset(token)


def _current_name() -> str | None:
    return _request_agent_name.get() or _agent_name or os.getenv("AGENT_NAME")


def get_agent_name() -> str:
    name = _current_name()
    if not name:
        raise RuntimeError(
            "에이전트 이름이 없다: create_agent_app을 먼저 만들거나 AGENT_NAME을 설정한다"
        )
    return name


def is_mock(name: str | None = None) -> bool:
    """`AGENT_TEMPLATE_MOCK=1`이거나 `<부서>_MOCK=1`이면 목 모드다 (CI는 항상 1)."""
    if os.getenv("AGENT_TEMPLATE_MOCK") == "1":
        return True
    name = name or _current_name()
    return bool(name) and os.getenv(f"{env_prefix(name)}_MOCK") == "1"


def agent_token(name: str) -> str:
    """이 에이전트가 요청에 붙이는 토큰. 목 모드에서는 `mock:<이름>`이다."""
    token = os.getenv(f"{env_prefix(name)}_TOKEN")
    if token:
        return token
    if is_mock(name):
        return f"mock:{name}"
    raise RuntimeError(f"{env_prefix(name)}_TOKEN이 설정되지 않았다")


def agent_url(target: str) -> str:
    """대상 에이전트의 기본 주소. `http`로 시작하면 그대로 쓴다.

    TODO: 통합 에이전트의 Agent Card 레지스트리로 대체한다.
    """
    if target.startswith("http"):
        return target.rstrip("/")
    override = os.getenv(f"AX_{env_prefix(target)}_URL")
    if override:
        return override.rstrip("/")
    port = DEFAULT_PORTS.get(target)
    if port is None:
        raise KeyError(f"알 수 없는 에이전트: {target}")
    return f"http://{target}:{port}"
