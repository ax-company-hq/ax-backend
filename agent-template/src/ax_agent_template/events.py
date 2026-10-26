"""이벤트 발행·구독 (Redis Streams).

모든 서비스(shop-api 포함)는 `publish`로만 이벤트를 발행한다. 별도 HTTP 이벤트 엔드포인트를
만들지 않는다. 받는 쪽은 같은 `event_id`를 두 번 처리하지 않고, 발행 쪽은 outbox로 재전송한다
(최소 한 번 전달). 이벤트에는 사실만 담고 판단 문장은 넣지 않는다.

두 가지 모드가 있다.
- 목 모드(`AGENT_TEMPLATE_MOCK=1`): 프로세스 안의 메모리 버스. 테스트·로컬 목 시연용.
- 실제 모드(`REDIS_URL`이 있고 목 모드가 아님): Redis Streams.
  - 스트림 하나(`AX_EVENT_STREAM`, 기본 `ax:events`)에 봉투 JSON을 `XADD`한다.
  - 서비스마다 소비 그룹(그룹 이름 = 에이전트 이름)을 두어 모든 서비스가 모든 이벤트를 받고,
    종류는 받는 쪽 핸들러가 거른다. 그룹 안에서는 소비자 하나가 한 번에 한 건만 맡는다.
  - 핸들러가 끝나야 `XACK`한다. 실패하거나 프로세스가 죽으면 남은 건을 다시 받고(재시작 포함),
    다른 소비자가 죽은 건은 `XAUTOCLAIM`으로 이어받는다. 같은 건이 `MAX_DELIVERIES`번 실패하면
    죽은 편지함(`<스트림>:dead`)으로 옮기고 로그를 남긴 뒤 넘어간다.
  - 같은 `event_id`는 발행 때 한 번만 쌓는다(`SET NX`, 1일 보관). 받는 쪽은 최소 한 번 전달을
    전제로 `event_id`로 중복을 걸러야 한다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from ax_agent_template.config import get_agent_name, is_mock
from ax_agent_template.logging import log_event
from ax_contracts.schemas import EventEnvelope, EventType

EventHandler = Callable[[EventEnvelope], Awaitable[None]]

logger = logging.getLogger("ax.events")

STREAM_DEFAULT = "ax:events"
ID_KEY_PREFIX = "ax:event-id:"
ID_TTL_SECONDS = 24 * 3600
STREAM_MAXLEN = 100_000  # 대략의 상한(근사 트리밍)
READ_COUNT = 10
BLOCK_MS = 1000
CLAIM_IDLE_MS = 30_000  # 이 시간 넘게 처리되지 않은 건은 이어받는다
MAX_DELIVERIES = 5  # 이만큼 실패하면 죽은 편지함으로 옮긴다


class EventBusUnavailable(NotImplementedError):
    """실제 모드인데 `REDIS_URL`이 없다. 기존 `except NotImplementedError`가 그대로 받는다."""


# 목 모드 메모리 버스
MOCK_PUBLISHED: list[EventEnvelope] = []
# 목 모드와 실제 모드가 같이 쓰는 구독 목록
_subscribers: list[tuple[frozenset[EventType], EventHandler]] = []
_redis_client: Any = None
_consumer_task: asyncio.Task[None] | None = None


def stream_name() -> str:
    return os.getenv("AX_EVENT_STREAM") or STREAM_DEFAULT


def redis_configured() -> bool:
    return bool(os.getenv("REDIS_URL")) or _redis_client is not None


def set_redis_client(client: Any) -> None:
    """테스트에서 fakeredis 같은 클라이언트를 넣는다(None이면 `REDIS_URL`로 새로 만든다)."""
    global _redis_client
    _redis_client = client


def _redis() -> Any:
    global _redis_client
    if _redis_client is None:
        url = os.getenv("REDIS_URL")
        if not url:
            raise EventBusUnavailable("REDIS_URL이 없어 이벤트 버스(Redis Streams)를 쓸 수 없다")
        from redis.asyncio import Redis

        _redis_client = Redis.from_url(url, decode_responses=True)
    return _redis_client


def reset_mock_bus() -> None:
    """테스트 사이에 버스 상태를 비운다(목 메모리, 구독 목록, 소비 작업, Redis 클라이언트)."""
    global _redis_client, _consumer_task
    MOCK_PUBLISHED.clear()
    _subscribers.clear()
    if _consumer_task is not None and not _consumer_task.done():
        try:
            _consumer_task.cancel()
        except RuntimeError:  # 이벤트 루프가 이미 닫힘
            pass
    _consumer_task = None
    _redis_client = None


def _log_published(envelope: EventEnvelope) -> None:
    """발행에 성공한 이벤트를 공통 로그에 한 줄 남긴다 (#330).

    payload에는 종류와 `event_id`만 담고 `data`·`untrusted`는 옮기지 않는다. 사건이 아닌
    이벤트(`context_id` 없음)는 건너뛴다. 로그 실패는 `log_event`가 삼키므로 발행을 막지 않는다.
    """
    if envelope.context_id is None:
        return
    log_event(
        envelope.context_id,
        envelope.tenant_id,
        "event_published",
        {"event_type": envelope.type.value, "event_id": envelope.event_id},
        source=envelope.source,
    )


async def publish(envelope: EventEnvelope) -> None:
    """이벤트를 발행한다. 같은 `event_id`는 한 번만 전달한다."""
    if not is_mock():
        await _publish_redis(envelope)
        return
    if any(sent.event_id == envelope.event_id for sent in MOCK_PUBLISHED):
        return
    MOCK_PUBLISHED.append(envelope)
    _log_published(envelope)
    for types, handler in list(_subscribers):
        if envelope.type in types:
            await handler(envelope)


async def _publish_redis(envelope: EventEnvelope) -> None:
    client = _redis()
    key = ID_KEY_PREFIX + envelope.event_id
    if not await client.set(key, "1", nx=True, ex=ID_TTL_SECONDS):
        return  # 이미 발행한 event_id
    try:
        await client.xadd(
            stream_name(),
            {"type": envelope.type.value, "envelope": envelope.model_dump_json()},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        await client.delete(key)  # 쌓이지 못했으니 다시 발행할 수 있게 푼다
        raise
    _log_published(envelope)


def subscribe(types: list[EventType], handler: EventHandler) -> None:
    """지정한 종류의 이벤트를 받을 핸들러를 등록한다.

    실제 모드에서는 소비 작업이 돌아야 받는다. `create_agent_app`으로 만든 앱은 시작할 때
    `start_consumer()`를 부르므로 따로 할 일이 없다. 그 밖의 서버는 시작할 때 직접 부른다.
    """
    if not is_mock() and not redis_configured():
        raise EventBusUnavailable("REDIS_URL이 없어 이벤트를 구독할 수 없다")
    _subscribers.append((frozenset(types), handler))


def has_subscribers() -> bool:
    return bool(_subscribers)


def start_consumer() -> asyncio.Task[None] | None:
    """실제 모드의 소비 작업을 시작한다. 목 모드이거나 구독이 없거나 이미 돌면 아무 일도 안 한다."""
    global _consumer_task
    if is_mock() or not _subscribers or not redis_configured():
        return None
    if _consumer_task is not None and not _consumer_task.done():
        return _consumer_task
    _consumer_task = asyncio.get_running_loop().create_task(_consume_forever(), name="ax-events")
    return _consumer_task


async def stop_consumer() -> None:
    global _consumer_task
    task, _consumer_task = _consumer_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _group() -> str:
    return get_agent_name()


def _consumer_name() -> str:
    return f"{get_agent_name()}-{socket.gethostname()}-{os.getpid()}"


async def _ensure_group(client: Any) -> None:
    from redis.exceptions import ResponseError

    try:
        await client.xgroup_create(stream_name(), _group(), id="0", mkstream=True)
    except ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


async def _dispatch(envelope: EventEnvelope) -> None:
    for types, handler in list(_subscribers):
        if envelope.type in types:
            await handler(envelope)


async def _handle_entry(client: Any, entry_id: str, fields: dict[str, str]) -> None:
    """한 건을 처리하고 끝나면 ack한다. 실패하면 ack하지 않아 다시 받게 둔다.

    구독하지 않은 종류는 봉투를 검증하기 전에 종류 문자열만 보고 ack한다. 그래서 새 이벤트 종류가
    계약에 더해져도 아직 모르는 소비자가 검증 실패로 죽은 편지함에 쌓지 않는다.
    """
    if fields.get("type") not in {t.value for types, _ in _subscribers for t in types}:
        await client.xack(stream_name(), _group(), entry_id)
        return
    try:
        envelope = EventEnvelope.model_validate_json(fields["envelope"])
    except Exception:  # 읽을 수 없는 건은 다시 받아도 같으니 죽은 편지함으로
        await _dead_letter(client, entry_id, fields, "읽을 수 없는 이벤트")
        return
    try:
        await _dispatch(envelope)
    except Exception:
        logger.exception(
            "이벤트 처리 실패 %s %s (다시 받는다)", envelope.type.value, envelope.event_id
        )
        await _maybe_dead_letter(client, entry_id, fields)
        return
    await client.xack(stream_name(), _group(), entry_id)


async def _delivery_count(client: Any, entry_id: str) -> int:
    rows = await client.xpending_range(stream_name(), _group(), min=entry_id, max=entry_id, count=1)
    return int(rows[0]["times_delivered"]) if rows else 0


async def _maybe_dead_letter(client: Any, entry_id: str, fields: dict[str, str]) -> None:
    if await _delivery_count(client, entry_id) >= MAX_DELIVERIES:
        await _dead_letter(client, entry_id, fields, f"{MAX_DELIVERIES}번 처리하지 못함")


async def _dead_letter(client: Any, entry_id: str, fields: dict[str, str], reason: str) -> None:
    await client.xadd(stream_name() + ":dead", {**fields, "reason": reason, "group": _group()})
    await client.xack(stream_name(), _group(), entry_id)
    logger.error("이벤트를 죽은 편지함으로 옮김 %s: %s", entry_id, reason)


async def poll_once(*, block_ms: int = 0) -> int:
    """한 번 읽어 처리한다. 먼저 남의 몫으로 오래 걸린 건과 내 몫으로 남은 건(재시작·실패)을,
    그다음 새 건을 받는다. 처리한 건수를 돌려준다. 테스트와 소비 루프가 같이 쓴다.
    """
    client = _redis()
    await _ensure_group(client)
    stream, group, name = stream_name(), _group(), _consumer_name()
    handled = 0
    claimed = await client.xautoclaim(
        stream, group, name, min_idle_time=CLAIM_IDLE_MS, count=READ_COUNT
    )
    for entry_id, fields in claimed[1]:
        if fields:
            await _handle_entry(client, entry_id, fields)
            handled += 1
    pending = await client.xreadgroup(group, name, {stream: "0"}, count=READ_COUNT)
    for _, entries in pending or []:
        for entry_id, fields in entries:
            if fields:
                await _handle_entry(client, entry_id, fields)
                handled += 1
    fresh = await client.xreadgroup(
        group, name, {stream: ">"}, count=READ_COUNT, block=block_ms or None
    )
    for _, entries in fresh or []:
        for entry_id, fields in entries:
            await _handle_entry(client, entry_id, fields)
            handled += 1
    return handled


async def _consume_forever() -> None:
    backoff = 1.0
    while True:
        try:
            handled = await poll_once(block_ms=BLOCK_MS)
            backoff = 1.0
            if not handled and not BLOCK_MS:  # 막고 기다리지 않는 설정이면 잠깐 쉰다
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("이벤트 소비 실패, %.0f초 뒤 다시 시도한다", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
