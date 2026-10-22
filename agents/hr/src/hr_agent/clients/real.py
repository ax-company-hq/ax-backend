"""실제 연동 클라이언트: planning은 A2A, memory는 HTTP로 부른다(3단계에서 켠다).

기본은 목이고, `HR_PLANNING_MODE=a2a`·`HR_MEMORY_MODE=http`일 때만 쓴다. 인터페이스는
`clients/planning.py`·`clients/memory.py`의 Protocol과 같아서 서비스 코드는 바뀌지 않는다.

가정(상대 쪽이 아직 없어서 정한 것 — 확인되면 이 파일만 고친다):
- planning: Agent Card의 skill 이름이 계약과 같다(`list_in_progress`, `verify_handover`), 입출력은
  `contracts/.../planning.py`의 모델이다. 호출은 템플릿 `call_agent`(A2A `POST /a2a`).
- memory: REST `HR_MEMORY_URL`(기본 `AX_MEMORY_URL`, 없으면 http://localhost:8002) 아래
  `POST /api/memory/cards`(저장), `GET /api/memory/cards/{id}`, `GET /api/memory/cards`(목록, 쿼리
  area·states), `GET /api/memory/cards/search`(검색, 쿼리 q·area·states·kinds·limit),
  `PATCH /api/memory/cards/{id}/status`, `PATCH /api/memory/cards/{id}/candidate`.
  응답은 `{"data": ...}` 봉투다(공통 구성안 API 형식). 실제 이름·경로는 재현의 memory가 정한다.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from ax_agent_template.client import call_agent
from ax_contracts.schemas import (
    AgentRequest,
    Card,
    ListInProgressInput,
    ListInProgressOutput,
    RequestMeta,
    TaskStatus,
    VerifyHandoverInput,
    VerifyHandoverOutput,
)
from hr_agent.clients.errors import UpstreamError, current_meta
from hr_agent.clients.memory import CardHit

CALL_TIMEOUT_SECONDS = 20


class A2APlanningClient:
    """planning에 `list_in_progress`·`verify_handover`를 A2A로 요청한다(둘 다 조회·검증이다)."""

    def __init__(
        self, target: str = "planning", http_client: httpx.AsyncClient | None = None
    ) -> None:
        self._target = target
        self._http = http_client

    async def _call(self, skill: str, payload: dict[str, Any]) -> dict[str, Any]:
        base = current_meta()
        if base is None:
            raise UpstreamError("planning", "처리 중인 요청의 메타가 없다")
        deadline = min(base.deadline, datetime.now(UTC) + timedelta(seconds=CALL_TIMEOUT_SECONDS))
        meta = RequestMeta(
            context_id=base.context_id,
            tenant_id=base.tenant_id,
            token="replaced-by-call_agent",
            path=list(base.path),
            deadline=deadline,
        )
        result = await call_agent(
            self._target,
            AgentRequest(meta=meta, skill=skill, input=payload),
            http_client=self._http,
        )
        if result.status != TaskStatus.COMPLETED:
            raise UpstreamError(
                "planning", f"{skill}: {result.status.value} {result.outcome or ''}".strip()
            )
        return result.output

    async def list_in_progress(self, query: ListInProgressInput) -> ListInProgressOutput:
        raw = await self._call("list_in_progress", query.model_dump(mode="json"))
        try:
            return ListInProgressOutput.model_validate(raw)
        except ValidationError as exc:
            raise UpstreamError("planning", "list_in_progress 응답 형식 오류") from exc

    async def verify_handover(self, query: VerifyHandoverInput) -> VerifyHandoverOutput:
        raw = await self._call("verify_handover", query.model_dump(mode="json"))
        try:
            return VerifyHandoverOutput.model_validate(raw)
        except ValidationError as exc:
            raise UpstreamError("planning", "verify_handover 응답 형식 오류") from exc


class HttpMemoryClient:
    """memory 서비스에 카드를 쓰고 읽는다. 쓰기는 호출하는 쪽이 `execute()` 뒤에만 부른다."""

    def __init__(
        self, base_url: str | None = None, http_client: httpx.AsyncClient | None = None
    ) -> None:
        url = base_url or os.getenv("HR_MEMORY_URL") or os.getenv("AX_MEMORY_URL")
        self._base = (url or "http://localhost:8002").rstrip("/")
        self._http = http_client

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        client = self._http or httpx.AsyncClient()
        try:
            response = await client.request(
                method,
                f"{self._base}{path}",
                json=json,
                params=params,
                timeout=CALL_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError("memory", f"{method} {path}: {type(exc).__name__}") from exc
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise UpstreamError("memory", f"{method} {path}: http {response.status_code}")
        try:
            return response.json().get("data")
        except (ValueError, AttributeError) as exc:
            raise UpstreamError("memory", f"{method} {path}: 응답이 JSON이 아니다") from exc

    @staticmethod
    def _card(raw: Any) -> Card:
        try:
            return Card.model_validate(raw)
        except ValidationError as exc:
            raise UpstreamError("memory", "카드 형식 오류") from exc

    async def write_card(self, card: Card) -> str:
        data = await self._request("POST", "/api/memory/cards", json=card.model_dump(mode="json"))
        found = data.get("card_id") if isinstance(data, dict) else None
        if not found:
            raise UpstreamError("memory", "카드 저장 응답에 card_id가 없다")
        return str(found)

    async def get_card(self, card_id: str) -> Card | None:
        data = await self._request("GET", f"/api/memory/cards/{card_id}")
        return None if data is None else self._card(data)

    async def list_cards(
        self, *, area: str | None = None, states: list[str] | None = None
    ) -> list[Card]:
        params: dict[str, Any] = {}
        if area:
            params["area"] = area
        if states:
            params["states"] = ",".join(states)
        data = await self._request("GET", "/api/memory/cards", params=params)
        return [self._card(item) for item in (data or [])]

    async def search_cards(
        self,
        query: str,
        *,
        area: str | None = None,
        kinds: list[str] | None = None,
        states: list[str] | None = None,
        limit: int = 5,
    ) -> list[CardHit]:
        params: dict[str, Any] = {"q": query, "limit": limit}
        if area:
            params["area"] = area
        if kinds:
            params["kinds"] = ",".join(kinds)
        if states:
            params["states"] = ",".join(states)
        data = await self._request("GET", "/api/memory/cards/search", params=params)
        hits = []
        for item in data or []:
            try:
                hits.append(CardHit(self._card(item["card"]), float(item["score"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise UpstreamError("memory", "검색 결과 형식 오류") from exc
        return hits

    async def set_status(self, card_id: str, status: str, superseded_by: str | None = None) -> None:
        await self._request(
            "PATCH",
            f"/api/memory/cards/{card_id}/status",
            json={"status": status, "superseded_by": superseded_by},
        )

    async def set_candidate(self, card_id: str, candidate: bool) -> None:
        await self._request(
            "PATCH", f"/api/memory/cards/{card_id}/candidate", json={"candidate": candidate}
        )
