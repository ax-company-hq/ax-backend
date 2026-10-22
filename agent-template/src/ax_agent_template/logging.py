"""사건 단위 로그. 모든 로그에 `context_id`와 `tenant_id`를 붙인다.

TODO: 공통 로그 저장소로 보낸다(대시보드 서버는 이 저장소만 읽는다). 지금은 표준 로거
`ax.event`에 JSON 한 줄로 남긴다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger("ax.event")


def log_event(context_id: str, tenant_id: str, kind: str, payload: dict[str, Any]) -> None:
    """사건 로그 한 줄을 남긴다. `kind`는 로그 종류(예: `request_received`)다."""
    _logger.info(
        json.dumps(
            {"context_id": context_id, "tenant_id": tenant_id, "kind": kind, "payload": payload},
            ensure_ascii=False,
            default=str,
        )
    )
