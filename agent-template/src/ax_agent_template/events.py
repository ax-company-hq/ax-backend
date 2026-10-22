"""이벤트 발행·구독 (Redis Streams).

모든 서비스(shop-api 포함)는 `publish`로만 이벤트를 발행한다. 별도 HTTP 이벤트 엔드포인트를
만들지 않는다. 받는 쪽은 같은 `event_id`를 두 번 처리하지 않고, 발행 쪽은 outbox로 재전송한다
(최소 한 번 전달). 이벤트에는 사실만 담고 판단 문장은 넣지 않는다.

TODO: Redis Streams 연동(`XADD`·`XREADGROUP`). 지금은 목 모드(`AGENT_TEMPLATE_MOCK=1`)에서만
메모리 버스로 동작하고, 그 밖에는 `NotImplementedError`다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ax_agent_template.config import is_mock
from ax_contracts.schemas import EventEnvelope, EventType

EventHandler = Callable[[EventEnvelope], Awaitable[None]]

# 목 모드 메모리 버스
MOCK_PUBLISHED: list[EventEnvelope] = []
_mock_subscribers: list[tuple[frozenset[EventType], EventHandler]] = []


def reset_mock_bus() -> None:
    """테스트 사이에 목 버스를 비운다."""
    MOCK_PUBLISHED.clear()
    _mock_subscribers.clear()


async def publish(envelope: EventEnvelope) -> None:
    """이벤트를 발행한다. 같은 `event_id`는 한 번만 전달한다."""
    if not is_mock():
        raise NotImplementedError("TODO: Redis Streams XADD")
    if any(sent.event_id == envelope.event_id for sent in MOCK_PUBLISHED):
        return
    MOCK_PUBLISHED.append(envelope)
    for types, handler in list(_mock_subscribers):
        if envelope.type in types:
            await handler(envelope)


def subscribe(types: list[EventType], handler: EventHandler) -> None:
    """지정한 종류의 이벤트를 받을 핸들러를 등록한다."""
    if not is_mock():
        raise NotImplementedError("TODO: Redis Streams XREADGROUP")
    _mock_subscribers.append((frozenset(types), handler))
