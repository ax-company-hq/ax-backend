"""에이전트 서버 뼈대: Agent Card 공개와 A2A 요청 수신.

`create_agent_app(card, handlers)`가 FastAPI 앱을 만든다.

- `GET /.well-known/agent-card.json`: Agent Card
- `GET /health`: `{"data": {"status": "ok"}, "meta": {}}`
- `POST /a2a`: `AgentRequest`를 받아 `AgentResult`를 돌려준다

수신 순서 (각 단계는 아래의 작은 함수이고, 내부 로직 중 TODO는 아직 스텁이다):

1. `context_id`·`tenant_id` 확인 → 없으면 `MISSING_CONTEXT`
2. 토큰 검증 → 실패하면 `UNAUTHORIZED` (`requester`는 토큰에서 꺼내고 본문 값은 무시한다)
3. (새 요청일 때만) `path`에 자기 이름이 있으면 `LOOP_DETECTED`
4. `path` 길이가 3을 넘으면 이관 큐에 넣고 `HOP_LIMIT`
5. `allowed_requesters` 확인 → 없으면 `FORBIDDEN_SKILL`
6. 마감(`deadline`)이 지났으면 실행하지 않고 `canceled`(`TIMEOUT`)
7. 핸들러 호출
8. `AgentResult` 스키마 검증 → 깨지면 `failed`(`INVALID_OUTPUT`)

루프 검사는 새 요청에만 적용한다. 동기 질의의 응답은 같은 Task의 결과라서 검사하지 않는다
(이 서버는 요청만 받으므로 응답은 HTTP 응답으로 돌려주는 값이고 별도 검사 대상이 아니다).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ax_agent_template.config import MAX_PATH_LENGTH, agent_name_scope, is_mock, set_agent_name
from ax_agent_template.logging import log_event
from ax_contracts.schemas import (
    AgentCard,
    AgentRequest,
    AgentResult,
    ErrorCode,
    Escalation,
    RequestMeta,
    TaskStatus,
)

logger = logging.getLogger("ax.template.server")

Handler = Callable[[AgentRequest], Awaitable[AgentResult | dict[str, Any]]]

# 목 모드에서 이관 큐 대신 쌓아 두는 곳 (테스트에서 확인용)
MOCK_ESCALATIONS: list[Escalation] = []


def error_result(
    meta: RequestMeta,
    status: TaskStatus,
    code: ErrorCode | str,
    message: str = "",
) -> AgentResult:
    """거부·취소·실패 결과를 만든다. `outcome`에 `ErrorCode` 값을 담는다."""
    code_value = code.value if isinstance(code, ErrorCode) else code
    return AgentResult(
        meta=meta,
        status=status,
        outcome=code_value,
        output={"message": message} if message else {},
    )


def verify_token(token: str) -> str:
    """토큰을 검증하고 요청자(에이전트 이름)를 돌려준다.

    TODO: 실제 에이전트 토큰 검증. 지금은 목 모드에서만 `mock:<이름>`을 받는다.
    목 모드가 아니면 거부한다(fail-closed).
    """
    if is_mock() and token.startswith("mock:") and len(token) > len("mock:"):
        return token.removeprefix("mock:")
    raise PermissionError("토큰을 검증할 수 없다")


async def enqueue_escalation(escalation: Escalation) -> None:
    """이관 큐에 넣는다. TODO: 통합 에이전트의 이관 큐 연동. 목 모드는 메모리에 쌓는다."""
    if is_mock():
        MOCK_ESCALATIONS.append(escalation)
        return
    raise NotImplementedError("TODO: 이관 큐 연동")


def check_loop(card: AgentCard, meta: RequestMeta) -> bool:
    """새 요청의 `path`에 자기 이름이 있으면 루프다."""
    return card.name in meta.path


def check_hop_limit(meta: RequestMeta) -> bool:
    return len(meta.path) > MAX_PATH_LENGTH


def check_acl(card: AgentCard, skill_id: str, requester: str) -> bool:
    """skill이 카드에 있고, 요청자가 `allowed_requesters`에 있는지 확인한다."""
    for skill in card.skills:
        if skill.id == skill_id:
            return requester in skill.allowed_requesters
    return False


def check_deadline(meta: RequestMeta, now: datetime | None = None) -> bool:
    """마감이 지났으면 False."""
    return (now or datetime.now(UTC)) < meta.deadline


def check_idempotency_key(card: AgentCard, request: AgentRequest) -> None:
    """TODO: 상태 변경 skill(`grade`가 `read`가 아님)에는 `idempotency_key`가 필수다.

    같은 키의 두 번째 요청은 이전 결과를 돌려준다. 저장소 연동 전이라 아직 검사하지 않는다.
    """


def _bad_request(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"code": code, "message": message}})


def create_agent_app(card: AgentCard, handlers: Mapping[str, Handler]) -> FastAPI:
    """Agent Card와 skill 핸들러로 에이전트 앱을 만든다.

    `handlers`의 키는 skill id이고, 카드의 skill과 정확히 같아야 한다(어긋나면 시작할 때 실패).
    핸들러는 `async def handler(request: AgentRequest) -> AgentResult`다.
    """
    skill_ids = {skill.id for skill in card.skills}
    if set(handlers) != skill_ids:
        missing = sorted(skill_ids - set(handlers))
        extra = sorted(set(handlers) - skill_ids)
        raise ValueError(f"카드와 핸들러가 다르다: 핸들러 없음={missing}, 카드에 없음={extra}")

    set_agent_name(card.name)
    app = FastAPI(title=f"ax-agent-{card.name}")

    @app.get("/health")
    def health() -> dict:
        return {"data": {"status": "ok"}, "meta": {}}

    @app.get("/.well-known/agent-card.json")
    def agent_card() -> dict:
        return card.model_dump(mode="json")

    @app.post("/a2a")
    async def receive(http_request: Request) -> JSONResponse:
        try:
            raw = await http_request.json()
        except ValueError:
            return _bad_request("INVALID_REQUEST", "JSON이 아니다")

        meta_raw = raw.get("meta") if isinstance(raw, dict) else None
        if not isinstance(meta_raw, dict) or not meta_raw.get("context_id"):
            return _bad_request(ErrorCode.MISSING_CONTEXT.value, "context_id가 없다")
        if not meta_raw.get("tenant_id", "shop-demo"):
            return _bad_request(ErrorCode.MISSING_CONTEXT.value, "tenant_id가 비었다")
        try:
            request = AgentRequest.model_validate(raw)
        except ValidationError as exc:
            return _bad_request("INVALID_REQUEST", str(exc))

        with agent_name_scope(card.name):
            result = await _handle(card, handlers, request)
        return JSONResponse(content=result.model_dump(mode="json"))

    return app


def _skill_grade(card: AgentCard, skill_id: str) -> str | None:
    for skill in card.skills:
        if skill.id == skill_id:
            return skill.grade
    return None


def _verified_requester(meta: RequestMeta) -> str | None:
    """로그용 요청자. 토큰에서만 꺼내고 본문 값은 쓰지 않는다. 검증에 실패하면 `None`이다."""
    try:
        return verify_token(meta.token)
    except PermissionError:
        return None


def _execution_id(result: AgentResult) -> str | None:
    value = result.output.get("execution_id")
    return value if isinstance(value, str) and len(value) <= 64 else None


async def _handle(
    card: AgentCard, handlers: Mapping[str, Handler], request: AgentRequest
) -> AgentResult:
    """요청을 처리하고 받은 것·보낸 것을 공통 로그에 한 줄씩 남긴다(COM-08).

    `source`는 이 에이전트, `requester`는 요청한 쪽이다. 로그 `payload`에는 skill·상태·ID 같은
    사실만 담고 `input`·`untrusted`·판단 문장은 넣지 않는다.
    """
    meta = request.meta
    requester = _verified_requester(meta)
    grade = _skill_grade(card, request.skill)
    received: dict[str, Any] = {"skill": request.skill, "path": list(meta.path)}
    if meta.idempotency_key:
        received["idempotency_key"] = meta.idempotency_key
    log_event(
        meta.context_id,
        meta.tenant_id,
        "request_received",
        received,
        source=card.name,
        skill=request.skill,
        grade=grade,
        requester=requester,
    )
    result = await _process(card, handlers, request)
    sent: dict[str, Any] = {
        "skill": request.skill,
        "status": result.status.value,
        "outcome": result.outcome,
    }
    if execution_id := _execution_id(result):
        sent["execution_id"] = execution_id
    log_event(
        meta.context_id,
        meta.tenant_id,
        "result_sent",
        sent,
        source=card.name,
        skill=request.skill,
        task_status=result.status.value,
        grade=grade,
        requester=requester,
    )
    return result


async def _process(
    card: AgentCard, handlers: Mapping[str, Handler], request: AgentRequest
) -> AgentResult:
    meta = request.meta

    try:
        requester = verify_token(meta.token)
    except PermissionError:
        return error_result(meta, TaskStatus.REJECTED, ErrorCode.UNAUTHORIZED)
    meta = meta.model_copy(update={"requester": requester})  # 본문의 requester는 버린다
    request = request.model_copy(update={"meta": meta})

    if check_loop(card, meta):
        return error_result(meta, TaskStatus.REJECTED, ErrorCode.LOOP_DETECTED)

    if check_hop_limit(meta):
        escalation = Escalation(
            context_id=meta.context_id,
            tenant_id=meta.tenant_id,
            path=meta.path,
            reason=ErrorCode.HOP_LIMIT.value,
        )
        try:
            await enqueue_escalation(escalation)
        except NotImplementedError:
            logger.warning("이관 큐가 아직 연결되지 않았다: %s", escalation.context_id)
        return error_result(meta, TaskStatus.REJECTED, ErrorCode.HOP_LIMIT)

    if not check_acl(card, request.skill, requester):
        return error_result(meta, TaskStatus.REJECTED, ErrorCode.FORBIDDEN_SKILL)

    if not check_deadline(meta):
        return error_result(meta, TaskStatus.CANCELED, ErrorCode.TIMEOUT)

    check_idempotency_key(card, request)

    try:
        raw_result = await handlers[request.skill](request)
    except Exception:
        logger.exception("핸들러 오류: skill=%s context_id=%s", request.skill, meta.context_id)
        return error_result(meta, TaskStatus.FAILED, "handler_error")

    try:
        result = AgentResult.model_validate(
            raw_result.model_dump() if isinstance(raw_result, AgentResult) else raw_result
        )
    except ValidationError:
        return error_result(meta, TaskStatus.FAILED, ErrorCode.INVALID_OUTPUT)
    if result.meta.context_id != meta.context_id:
        return error_result(meta, TaskStatus.FAILED, ErrorCode.INVALID_OUTPUT)
    return result
