"""마케팅 `get_notice`로 공지 사실을 읽어 답변 근거로 바꾼다.

공지를 읽는 유일한 경로가 `get_notice`다. 공지 본문·사실·약속은 마케팅이 준 값 그대로 쓰고,
찾지 못하면(`found=false`·통신 실패) 근거를 만들지 않는다 ("확인할 수 없음").
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from support_agent.schemas import Source

logger = logging.getLogger("ax.support.notices")

SendFn = Callable[[str, AgentRequest], Awaitable[AgentResult]]


def to_source(view: dict[str, Any]) -> Source | None:
    """`NoticeView`(dict)를 근거 조각으로 바꾼다. 찾지 못했으면 None."""
    if not view.get("found") or not view.get("notice_id"):
        return None
    parts = [view.get("public_text") or ""]
    for promise in view.get("promises") or []:
        kind = "포인트" if promise.get("type") == "points" else "쿠폰"
        parts.append(f"약속: {kind} {promise.get('amount')}원 ({promise.get('condition')})")
    if view.get("valid_to"):
        parts.append(f"기간: ~{view['valid_to']}")
    if view.get("status") == "ended":
        parts.append("(종료된 공지)")
    return Source(
        id=f"notice:{view['notice_id']}",
        doc_id="notice",
        category="notice",
        title="공지",
        section=str(view.get("channel_code") or ""),
        text=" ".join(p for p in parts if p),
        score=1.0,
    )


async def fetch_source(request: AgentRequest, notice_id: str, send: SendFn) -> Source | None:
    """공지를 조회해 근거로 돌려준다. 실패는 근거 없음으로 처리한다."""
    notice_request = AgentRequest(
        meta=request.meta.model_copy(update={"idempotency_key": None}),
        skill="get_notice",
        input={"notice_id": notice_id},
    )
    try:
        reply = await send("marketing", notice_request)
    except Exception:
        logger.warning("get_notice 호출 실패", exc_info=True)
        return None
    if reply.status != TaskStatus.COMPLETED:
        return None
    return to_source(reply.output)
