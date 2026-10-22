"""ax-safety 서버 진입점 (포트 8001).

| 메서드·경로 | 용도 | 토큰 |
| --- | --- | --- |
| `POST /api/safety/executions` | 실행 관문 (`ExecuteRequest` → `ExecuteDecision`) | 에이전트 |
| `GET /api/safety/executions?context_id=` | 실행 기록 | 없음(조회) |
| `GET /api/safety/reviews?status=pending` | C 보류 큐 | 없음(조회) |
| `GET /api/safety/reviews/{review_id}` | 보류 건과 근거 리포트 | 없음(조회) |
| `POST /api/safety/reviews/{review_id}/decision` | 승인·거절 | **사람만** |
| `GET /api/safety/audit-logs?context_id=` | 감사 로그 | 없음(조회) |
| `GET /api/safety/thresholds`, `GET /api/safety/registry` | 기준표·레지스트리 | 없음(조회) |

`ExecuteRequest.requester`는 믿지 않는다. 토큰에서 꺼낸 이름과 다르면 403이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from ax_agent_template.config import set_agent_name
from ax_agent_template.logging import log_event
from ax_contracts.schemas import ExecuteRequest
from ax_safety.auth import agent_from_token, bearer, human_from_token
from ax_safety.gate import Gate, Publisher, Verifier
from ax_safety.rules import load_registry, load_thresholds
from ax_safety.store import Store
from ax_safety.verify import call_verifier, publish_safety_event

logger = logging.getLogger("ax.safety")

EXPIRE_INTERVAL_SECONDS = 15


class ReviewDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    note: str | None = None


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data, "meta": {}}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(
    gate: Gate | None = None,
    *,
    verifier: Verifier = call_verifier,
    publisher: Publisher = publish_safety_event,
    expire_interval: float | None = EXPIRE_INTERVAL_SECONDS,
) -> FastAPI:
    set_agent_name("safety")
    gate = gate or Gate(load_registry(), load_thresholds(), Store(), verifier, publisher)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_expire_loop(gate, expire_interval)) if expire_interval else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="ax-safety", lifespan=lifespan)
    app.state.gate = gate

    @app.get("/health")
    def health() -> dict:
        return {"data": {"status": "ok"}, "meta": {}}

    @app.post("/api/safety/executions")
    async def execute(
        http_request: Request, authorization: str | None = Header(default=None)
    ) -> JSONResponse:
        try:
            raw = await http_request.json()
            request = ExecuteRequest.model_validate(raw)
        except (ValueError, ValidationError) as exc:
            return _error(400, "INVALID_REQUEST", str(exc)[:500])
        try:
            requester = agent_from_token(bearer(authorization))
        except PermissionError as exc:
            gate.store.append_audit(
                gate._now(),
                request.context_id,
                request.tenant_id,
                "auth_rejected",
                detail={"claimed_requester": request.requester, "reason": str(exc)},
            )
            return _error(401, "UNAUTHORIZED", str(exc))
        if requester != request.requester:
            gate.store.append_audit(
                gate._now(),
                request.context_id,
                request.tenant_id,
                "requester_mismatch",
                detail={"claimed_requester": request.requester, "token_requester": requester},
            )
            return _error(403, "FORBIDDEN", "본문의 requester가 토큰과 다르다")

        decision = await gate.execute(request)
        log_event(
            request.context_id,
            request.tenant_id,
            "safety_decision",
            decision.model_dump(mode="json", exclude_none=True),
        )
        return JSONResponse(content=_ok(decision.model_dump(mode="json")))

    @app.get("/api/safety/executions")
    def list_executions(context_id: str | None = None) -> dict:
        records = gate.store.list_executions(context_id)
        return _ok([r.model_dump(mode="json") for r in records])

    @app.get("/api/safety/reviews")
    async def list_reviews(
        status: Literal["pending", "approved", "rejected", "expired"] | None = None,
    ) -> dict:
        await gate.expire_reviews()
        return _ok([r.model_dump(mode="json") for r in gate.store.list_reviews(status)])

    @app.get("/api/safety/reviews/{review_id}", response_model=None)
    async def get_review(review_id: str) -> dict | JSONResponse:
        await gate.expire_reviews()
        review = gate.store.get_review(review_id)
        if review is None:
            return _error(404, "NOT_FOUND", "보류 건이 없다")
        return _ok(review.model_dump(mode="json"))

    @app.post("/api/safety/reviews/{review_id}/decision", response_model=None)
    async def decide_review(
        review_id: str,
        body: ReviewDecisionBody,
        authorization: str | None = Header(default=None),
    ) -> dict | JSONResponse:
        try:
            person = human_from_token(bearer(authorization))
        except PermissionError as exc:
            return _error(403, "HUMAN_ONLY", str(exc))
        try:
            review = await gate.decide_review(review_id, body.decision, person, body.note)
        except KeyError:
            return _error(404, "NOT_FOUND", "보류 건이 없다")
        except ValueError as exc:
            return _error(409, "ALREADY_DECIDED", str(exc))
        return _ok(review.model_dump(mode="json"))

    @app.get("/api/safety/audit-logs")
    def audit_logs(context_id: str | None = None) -> dict:
        return _ok([e.model_dump(mode="json") for e in gate.store.list_audit(context_id)])

    @app.get("/api/safety/thresholds")
    def thresholds() -> dict:
        return _ok(gate.thresholds.model_dump(mode="json"))

    @app.get("/api/safety/registry")
    def registry() -> dict:
        return _ok(gate.registry.model_dump(mode="json"))

    return app


async def _expire_loop(gate: Gate, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await gate.expire_reviews()
        except Exception:
            logger.exception("보류 건 자동 거절 실패")


app = create_app()
