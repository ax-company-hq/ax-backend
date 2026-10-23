"""사람 토큰 읽기 (결산 확정·대사 메모처럼 사람이 하는 결정용).

형식은 안전 관문과 같다: `Authorization: Bearer <토큰>`.
- 설정된 토큰: `FINANCE_HUMAN_TOKENS="이름:토큰,이름:토큰"`(없으면 `SAFETY_HUMAN_TOKENS`를 읽는다.
  재무 화면은 안전 관문과 같은 사람 토큰 `VITE_SAFETY_HUMAN_TOKEN`을 쓴다)
- 목 모드에서는 `human:<이름>`도 받는다. 에이전트 토큰(`mock:`)은 항상 거부한다.

부서끼리 import하지 않으므로 safety의 `auth.py`를 쓰지 않고 같은 규칙을 여기 작게 다시 쓴다.
"""

from __future__ import annotations

import hmac
import os

from ax_agent_template.config import is_mock


def bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _configured() -> dict[str, str]:
    raw = os.getenv("FINANCE_HUMAN_TOKENS") or os.getenv("SAFETY_HUMAN_TOKENS") or ""
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        name, sep, token = item.strip().partition(":")
        if sep and name and token:
            pairs[token] = name
    return pairs


def human_from_authorization(authorization: str | None) -> str:
    """사람 이름. 토큰이 없거나 사람 토큰이 아니면 PermissionError."""
    token = bearer(authorization)
    if not token or token.startswith("mock:"):
        raise PermissionError("사람 토큰이 필요하다 (Authorization: Bearer human:<이름>)")
    for known, name in _configured().items():
        if hmac.compare_digest(known, token):
            return name
    if is_mock("finance") and token.startswith("human:") and len(token) > len("human:"):
        return token.removeprefix("human:")
    raise PermissionError("사람 토큰을 검증할 수 없다")
