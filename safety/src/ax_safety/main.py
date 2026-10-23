"""ax-safety 서버 진입점 (포트 8001).

| 메서드·경로 | 용도 | 토큰 |
| --- | --- | --- |
| `POST /api/safety/executions` | 실행 관문 (`ExecuteRequest` → `ExecuteDecision`) | 에이전트 |
| `GET /api/safety/executions?context_id=` | 실행 기록 | 없음(조회) |
| `GET /api/safety/reviews?status=pending` | C 보류 큐 | 없음(조회) |
| `GET /api/safety/reviews/{review_id}` | 보류 건과 근거 리포트 | 없음(조회) |
| `POST /api/safety/reviews/{review_id}/decision` | 승인·거절 | **사람만** |
| `GET /api/safety/audit-logs?context_id=` | 감사 로그 | 없음(조회) |
| `GET /api/safety/thresholds`, `GET /api/safety/registry` | 기준표(+`baseline`)·레지스트리 | 없음 |
| `POST /api/safety/threshold-changes` | 기준 바꾸기 요청 (안전 장치 → AI 의견) | 사람·finance |
| `GET /api/safety/threshold-changes` | 기준 바꾸기 기록 (최근 것부터) | 없음(조회) |
| `POST /api/safety/threshold-changes/{change_id}/decision` | 적용·거절 | **사람만** |

기준 바꾸기(위 POST 두 개, 저장 값 복원)는 `SAFETY_THRESHOLD_CHANGES=1`일 때만 쓴다(기본 꺼짐).
꺼져 있으면 POST는 403 `FEATURE_OFF`이고 GET 기록은 빈 목록이다.

`ExecuteRequest.requester`는 믿지 않는다. 토큰에서 꺼낸 이름과 다르면 403이다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from ax_agent_template.config import set_agent_name
from ax_agent_template.logging import log_event
from ax_contracts.schemas import ExecuteRequest
from ax_safety.auth import agent_from_token, bearer, human_from_token
from ax_safety.gate import Gate, Publisher, Reviewer, ThresholdReviewer, Verifier
from ax_safety.overrides import OverrideFile
from ax_safety.rules import load_registry, load_thresholds, threshold_changes_on
from ax_safety.store import Store
from ax_safety.verify import (
    call_reviewer,
    call_threshold_reviewer,
    call_verifier,
    publish_safety_event,
)

logger = logging.getLogger("ax.safety")

EXPIRE_INTERVAL_SECONDS = 15

# 대시보드(dashboard-web)가 보류 큐·실행 기록을 읽고 사람 결정을 보낸다
DEFAULT_CORS_ORIGINS = "http://localhost:3001,http://127.0.0.1:3001"


def cors_origins() -> list[str]:
    raw = os.getenv("SAFETY_CORS_ORIGINS", DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


class ReviewDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    note: str | None = None


class ThresholdChangeBody(BaseModel):
    """`reason`은 사람이 쓴 글이라 믿지 않는다. 길이·빈 값은 안전 장치가 쉬운 말로 거절한다."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    new_value: StrictInt
    reason: str = Field(default="", max_length=2000)


# 기준 바꾸기를 요청할 수 있는 에이전트 (사람 토큰은 항상 허용)
THRESHOLD_CHANGE_AGENTS = {"finance"}

FEATURE_OFF_MESSAGE = "기준 바꾸기는 기준 문서 변경(docs PR) 머지 후 켭니다"


def _ok(data: Any) -> dict[str, Any]:
    return {"data": data, "meta": {}}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def create_app(
    gate: Gate | None = None,
    *,
    verifier: Verifier = call_verifier,
    publisher: Publisher = publish_safety_event,
    reviewer: Reviewer | None = call_reviewer,
    threshold_reviewer: ThresholdReviewer | None = call_threshold_reviewer,
    expire_interval: float | None = EXPIRE_INTERVAL_SECONDS,
) -> FastAPI:
    set_agent_name("safety")
    gate = gate or Gate(
        load_registry(),
        load_thresholds(),
        Store(),
        verifier,
        publisher,
        reviewer=reviewer,
        threshold_reviewer=threshold_reviewer,
        overrides=OverrideFile.from_env(),  # 바뀐 기준 저장·복원
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_expire_loop(gate, expire_interval)) if expire_interval else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="ax-safety", lifespan=lifespan)
    app.state.gate = gate
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins(),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

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
        """지금 쓰는 기준표와 `baseline`(TOML 원래 값, 바꿀 수 있는 기준만)."""
        return _ok({**gate.thresholds.model_dump(mode="json"), "baseline": gate.baseline_values()})

    @app.post("/api/safety/threshold-changes", response_model=None)
    async def request_threshold_change(
        body: ThresholdChangeBody, authorization: str | None = Header(default=None)
    ) -> dict | JSONResponse:
        if not threshold_changes_on():
            return _error(403, "FEATURE_OFF", FEATURE_OFF_MESSAGE)
        token = bearer(authorization)
        by_agent = False
        try:
            requested_by = human_from_token(token)
        except PermissionError:
            try:
                requested_by = agent_from_token(token)
            except PermissionError as exc:
                return _error(401, "UNAUTHORIZED", str(exc))
            if requested_by not in THRESHOLD_CHANGE_AGENTS:
                return _error(403, "FORBIDDEN", "기준 바꾸기는 사람이나 재무 AI만 요청할 수 있다")
            by_agent = True  # 에이전트 요청은 항상 사람이 적용한다
        change = await gate.request_threshold_change(
            body.key, body.new_value, body.reason, requested_by, by_agent=by_agent
        )
        log_event(
            f"ctx_req_{change.change_id}",
            "shop-demo",
            "threshold_change",
            change.model_dump(mode="json", exclude={"reason"}),
        )
        return _ok(change.model_dump(mode="json"))

    @app.get("/api/safety/threshold-changes")
    def list_threshold_changes() -> dict:
        if not threshold_changes_on():  # 꺼져 있으면 조회는 되지만 비어 있다
            return _ok([])
        return _ok([c.model_dump(mode="json") for c in gate.store.list_changes()])

    @app.post("/api/safety/threshold-changes/{change_id}/decision", response_model=None)
    async def decide_threshold_change(
        change_id: str,
        body: ReviewDecisionBody,
        authorization: str | None = Header(default=None),
    ) -> dict | JSONResponse:
        if not threshold_changes_on():
            return _error(403, "FEATURE_OFF", FEATURE_OFF_MESSAGE)
        try:
            person = human_from_token(bearer(authorization))
        except PermissionError as exc:
            return _error(403, "HUMAN_ONLY", str(exc))
        try:
            change = await gate.decide_threshold_change(change_id, body.decision, person, body.note)
        except KeyError:
            return _error(404, "NOT_FOUND", "기준 바꾸기 요청이 없다")
        except ValueError as exc:
            return _error(409, "ALREADY_DECIDED", str(exc))
        except OSError:
            logger.exception("바뀐 기준을 저장하지 못해 적용하지 않았다: %s", change_id)
            return _error(503, "SAVE_FAILED", "바뀐 기준을 저장하지 못해 적용하지 않았다")
        return _ok(change.model_dump(mode="json"))

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
