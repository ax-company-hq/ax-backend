"""응답 형식: 성공 `{data, meta}`, 실패 `{error: {code, message}}` (CLAUDE.md 표기·형식)."""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ax_contracts.schemas.common import TENANT_ID_DEFAULT


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


def ok(data: Any, total: int | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"tenant_id": os.getenv("TENANT_ID", TENANT_ID_DEFAULT)}
    if total is not None:
        meta["total"] = total
    return {"data": data, "meta": meta}


def is_mock() -> bool:
    return os.getenv("DASHBOARD_MOCK") == "1" or os.getenv("AGENT_TEMPLATE_MOCK") == "1"


def require_mock() -> None:
    """실제 데이터 연결은 3단계 이후다. 그 전에는 목 모드에서만 응답한다."""
    if not is_mock():
        raise ApiError(
            503,
            "UPSTREAM_NOT_CONNECTED",
            "데이터 출처가 아직 연결되지 않았다 (DASHBOARD_MOCK=1로 목 모드 사용)",
        )


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _error(422, "INVALID_REQUEST", f"{loc}: {first.get('msg', '잘못된 요청')}")
