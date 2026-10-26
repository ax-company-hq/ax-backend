"""쓰기 API 사람 토큰 확인 (#295).

형식은 안전 관문·재무와 같다: `Authorization: Bearer <토큰>`.
- 설정된 토큰: `DASHBOARD_HUMAN_TOKENS="이름:토큰,이름:토큰"`(없으면 `SAFETY_HUMAN_TOKENS`를 읽는다.
  화면은 안전 관문과 같은 사람 토큰 `VITE_SAFETY_HUMAN_TOKEN`을 쓴다)
- 목 모드에서는 `human:<이름>`도 받는다. 에이전트 토큰(`mock:`)은 항상 거부한다.
- 토큰이 설정되지 않았고 목 모드도 아니면 쓰기를 모두 거부한다 (fail-closed).

읽기(GET)는 그대로 열려 있다. 부서끼리 import하지 않으므로 safety의 `auth.py`를 쓰지 않고
같은 규칙을 여기 작게 다시 쓴다.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Request

from ax_dashboard_api.envelope import ApiError, is_mock

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 본문을 받아 계산만 하고 저장하지 않는 POST (읽기와 같다)
READ_ONLY_POSTS = frozenset({"/api/dashboard/plan-checks"})


def bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _configured() -> dict[str, str]:
    raw = os.getenv("DASHBOARD_HUMAN_TOKENS") or os.getenv("SAFETY_HUMAN_TOKENS") or ""
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        name, sep, token = item.strip().partition(":")
        if sep and name and token:
            pairs[token] = name
    return pairs


def human_from_authorization(authorization: str | None) -> str:
    """사람 이름. 토큰이 없으면 401, 에이전트 토큰이거나 모르는 토큰이면 403."""
    token = bearer(authorization)
    if not token:
        raise ApiError(401, "AUTH_REQUIRED", "사람 토큰이 필요하다 (Authorization: Bearer 토큰)")
    if token.startswith("mock:"):
        raise ApiError(403, "HUMAN_TOKEN_REQUIRED", "에이전트 토큰으로는 쓸 수 없다")
    for known, name in _configured().items():
        if hmac.compare_digest(known, token):
            return name
    if is_mock() and token.startswith("human:") and len(token) > len("human:"):
        return token.removeprefix("human:")
    raise ApiError(403, "HUMAN_TOKEN_INVALID", "사람 토큰을 검증할 수 없다")


def require_human_for_writes(request: Request) -> None:
    """상태를 바꾸는 요청(GET 외)에만 사람 토큰을 요구한다."""
    if request.method in SAFE_METHODS:
        return
    route = request.scope.get("route")
    if getattr(route, "path", None) in READ_ONLY_POSTS:
        return
    request.state.human = human_from_authorization(request.headers.get("authorization"))
