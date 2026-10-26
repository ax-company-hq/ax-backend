"""총괄(orchestrator) 읽기 중계. 관리자 화면(shop-web)이 shop-api만 부르게 한다.

- GET만 한다. 총괄의 사건 상태를 바꾸거나 승인하지 않는다(승인은 사람이 안전 관문 경로로 한다).
- 총괄 주소는 `SHOP_ORCHESTRATOR_URL`이다(기본 `http://127.0.0.1:8000`).
- 총괄이 꺼져 있거나 느려도 shop-api는 죽지 않고 `ShopError`로 답한다.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from ax_shop_api.service import ShopError

DEFAULT_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 5.0
CONTEXT_ID = re.compile(r"^ctx_inc_[0-9a-f]{1,32}$")
WINDOW_KEYS = ("window_seconds", "bucket_seconds")
TRANSPORT: httpx.AsyncBaseTransport | None = None  # 테스트에서 바꿀 수 있다


def base_url() -> str:
    return (os.getenv("SHOP_ORCHESTRATOR_URL") or DEFAULT_URL).rstrip("/")


async def _get(path: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            base_url=base_url(), timeout=TIMEOUT_SECONDS, transport=TRANSPORT
        ) as client:
            response = await client.get(path)
    except httpx.TimeoutException as error:
        raise ShopError("UPSTREAM_TIMEOUT", "총괄이 제때 응답하지 않았다", 504) from error
    except httpx.HTTPError as error:
        raise ShopError("UPSTREAM_UNAVAILABLE", "총괄에 연결할 수 없다", 502) from error
    if response.status_code == 404:
        raise ShopError("NOT_FOUND", "찾을 수 없다", 404)
    if response.status_code >= 400:
        raise ShopError("UPSTREAM_ERROR", f"총괄 호출 실패 ({response.status_code})", 502)
    try:
        body = response.json()
    except ValueError as error:
        raise ShopError("UPSTREAM_ERROR", "총괄 응답을 읽을 수 없다", 502) from error
    if not isinstance(body, dict) or "data" not in body:
        raise ShopError("UPSTREAM_ERROR", "총괄 응답 형식이 다르다", 502)
    return body


async def incidents() -> tuple[Any, dict[str, Any]]:
    """사건 목록. `(data, meta)`를 돌려준다."""
    body = await _get("/api/incidents")
    return body["data"], _meta(body)


async def incident(context_id: str) -> tuple[Any, dict[str, Any]]:
    if not CONTEXT_ID.match(context_id):
        raise ShopError("INVALID_CONTEXT_ID", "사건 번호 형식이 올바르지 않다", 400)
    body = await _get(f"/api/incidents/{context_id}")
    return body["data"], _meta(body)


async def departments() -> tuple[Any, dict[str, Any]]:
    """부서 카드·사용량. 총괄이 센 값을 그대로 전달한다(값을 만들거나 바꾸지 않는다)."""
    body = await _get("/api/departments")
    return body["data"], _meta(body, WINDOW_KEYS)


def _meta(body: dict[str, Any], numbers: tuple[str, ...] = ()) -> dict[str, Any]:
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return {}
    out: dict[str, Any] = {"mock": bool(meta["mock"])} if "mock" in meta else {}
    for key in numbers:  # 화면이 집계 구간을 말할 때 쓰는 값만 숫자일 때 전달한다
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    return out
