"""토큰 검증. 에이전트 토큰과 사람 토큰을 나눈다.

- 에이전트: `Authorization: Bearer <에이전트 토큰>`. 요청자는 토큰에서 꺼낸다
  (agent-template `verify_token`과 같은 규칙, 목 모드는 `mock:<이름>`).
- 사람: C 보류 결정은 사람 토큰만 받는다. `SAFETY_HUMAN_TOKENS="이름:토큰,이름:토큰"`.
  목 모드에서는 `human:<이름>`도 받는다. 에이전트 토큰은 항상 거부한다.
"""

from __future__ import annotations

import hmac
import os

from ax_agent_template.config import is_mock
from ax_agent_template.server import verify_token


def bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def agent_from_token(token: str | None) -> str:
    """에이전트 이름을 돌려준다. 검증할 수 없으면 PermissionError."""
    if not token:
        raise PermissionError("토큰이 없다")
    return verify_token(token)


def _human_tokens() -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in os.getenv("SAFETY_HUMAN_TOKENS", "").split(","):
        name, sep, token = item.strip().partition(":")
        if sep and name and token:
            pairs[token] = name
    return pairs


def human_from_token(token: str | None) -> str:
    """사람 이름을 돌려준다. 에이전트 토큰이거나 모르는 토큰이면 PermissionError."""
    if not token or token.startswith("mock:"):
        raise PermissionError("사람 토큰이 아니다")
    for known, name in _human_tokens().items():
        if hmac.compare_digest(known, token):
            return name
    if is_mock("safety") and token.startswith("human:") and len(token) > len("human:"):
        return token.removeprefix("human:")
    raise PermissionError("사람 토큰을 검증할 수 없다")
