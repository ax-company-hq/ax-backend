"""고객지원(support) skill 호출 (A2A). 클레임 접수·반품 입고가 쓴다.

- 요청자는 `shop-api`다. support의 `create_claim`·`receive_return` 요청 가능 목록에 들어 있다.
  이 앱의 기본 이름(`shop`)은 환경변수 접두사용이라, 호출하는 동안만 `shop-api`로 바꾼다.
- 요청마다 새 사건 ID `ctx_shop_<hex>`를 붙인다(`CLAIM_CONTEXT_PREFIX`). 계약 `ContextPrefix`에는
  쇼핑몰에서 시작한 클레임용 접수가 아직 없어 임시 값이다. 계약이 정하면 이 상수 한 곳만 바꾼다.
- 고객이 쓴 자유 문장은 `untrusted`로만 넘긴다.
- 결과의 `outcome`·`status`를 `ShopError`로 바꾼다(`unwrap`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ax_agent_template import config
from ax_agent_template.client import call_agent
from ax_contracts.schemas import AgentRequest, AgentResult, RequestMeta, TaskStatus
from ax_shop_api.service import ShopError

AGENT_NAME = "shop-api"
CLAIM_CONTEXT_PREFIX = "ctx_shop_"
TARGET = "support"
DEADLINE_SECONDS = 30

SendFn = Callable[[str, AgentRequest], Awaitable[AgentResult]]
SEND: SendFn = call_agent  # 테스트에서 바꿀 수 있다


def unwrap(result: AgentResult) -> dict[str, Any]:
    """결과를 출력 dict로 바꾸거나 `ShopError`로 올린다.

    고객지원은 중복 접수·없는 주문·잘못된 상태도 `status=completed`에 `outcome`만 달리해서 돌려준다
    (출력은 `{message}`뿐이다). 그래서 `outcome`을 먼저 오류로 바꾸고, 그 뒤에 `completed`를
    성공으로 본다.
    """
    outcome = str(result.outcome or "")
    message = str(result.output.get("message", "")) if result.output else ""
    if outcome == "not_found":
        raise ShopError("NOT_FOUND", message or "찾을 수 없다", 404)
    if outcome == "duplicate_claim":
        raise ShopError("DUPLICATE_CLAIM", message or "이미 접수된 클레임이 있다", 409)
    if outcome == "invalid_state":
        raise ShopError("INVALID_STATE", message or "현재 상태에서는 할 수 없다", 409)
    if outcome.startswith("not_executed_"):
        raise ShopError("NOT_EXECUTED", "실행되지 않았다 (승인 대기 또는 거부 상태)", 409)
    if result.status == TaskStatus.COMPLETED:
        return result.output
    if result.status == TaskStatus.CANCELED:
        raise ShopError("UPSTREAM_TIMEOUT", "고객지원이 제때 응답하지 않았다", 504)
    if result.status == TaskStatus.REJECTED:
        raise ShopError("UPSTREAM_REJECTED", f"고객지원이 요청을 거부했다 ({outcome})", 403)
    raise ShopError("UPSTREAM_ERROR", f"고객지원 호출 실패 ({outcome or result.status.value})", 502)


def new_context_id() -> str:
    return f"{CLAIM_CONTEXT_PREFIX}{uuid.uuid4().hex[:12]}"


async def call(
    skill: str,
    context_id: str,
    input: dict[str, Any],
    untrusted: dict[str, Any] | None = None,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    """support skill을 부른다. 상태를 바꾸는 skill이라 `target`으로 멱등 키를 만든다."""
    with config.agent_name_scope(AGENT_NAME):
        request = AgentRequest(
            meta=RequestMeta(
                context_id=context_id,
                token=config.agent_token(AGENT_NAME),
                deadline=datetime.now(UTC) + timedelta(seconds=DEADLINE_SECONDS),
                idempotency_key=f"{context_id}:{skill}:{target}" if target else None,
            ),
            skill=skill,
            input=input,
            untrusted=untrusted or {},
        )
        return unwrap(await SEND(TARGET, request))
