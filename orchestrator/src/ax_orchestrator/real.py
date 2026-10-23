"""실제 연동 부품: Agent Card 수집(HTTP)과 부서 호출(`call_agent`).

이벤트 구독(Redis Streams)은 agent-template `events.py`의 TODO라 아직 연결하지 않는다.
"""

from __future__ import annotations

from typing import Any

import httpx

from ax_agent_template.client import call_agent
from ax_agent_template.config import agent_url
from ax_orchestrator.registry import CardFetcher

CARD_PATH = "/.well-known/agent-card.json"
CARD_TIMEOUT = 5.0


def http_card_fetcher(client: httpx.AsyncClient | None = None) -> CardFetcher:
    """`GET <부서>/.well-known/agent-card.json`. 실패하면 None(레지스트리가 처리)."""

    async def fetch(dept: str) -> dict[str, Any] | None:
        http = client or httpx.AsyncClient()
        try:
            response = await http.get(agent_url(dept) + CARD_PATH, timeout=CARD_TIMEOUT)
            if response.status_code != 200:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None
        finally:
            if client is None:
                await http.aclose()

    return fetch


__all__ = ["call_agent", "http_card_fetcher"]
