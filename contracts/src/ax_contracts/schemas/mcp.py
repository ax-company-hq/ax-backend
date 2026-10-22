"""MCP 공통 봉투 (DejaFix common-mcp 원본과 같은 형식).

도구 이름은 `<대상>_<동사>_<대상>`(예: `shop_list_orders`, `memory_search_card`)이고,
모든 MCP 클라이언트는 목 모드(`<부서>_MOCK=1`)를 가진다. 쓰기 도구는 `execute()` 뒤에서만 부른다.
"""

from __future__ import annotations

from typing import Any

from ax_contracts.schemas.common import AxModel, UtcDatetime


class McpEnvelope(AxModel):
    ok: bool
    collected_at: UtcDatetime
    data: Any = None
    truncated: bool = False
