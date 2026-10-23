"""사건 단위 로그. 모든 로그에 `context_id`와 `tenant_id`를 붙인다.

두 곳에 남긴다.

1. 표준 로거 `ax.event`에 JSON 한 줄 (항상).
2. 공통 로그 저장소(`common_log.py`, COM-08). `kind`가 계약 `LogKind`에 있고 저장소가 설정되어
   있을 때만 쓴다. 대시보드 서버는 이 저장소만 읽는다.

로그를 남기다 실패해도 요청 처리는 멈추지 않는다(경고만 남긴다).
`payload`에는 코드가 만든 사실(skill·결과·ID·금액)만 담는다. 문의 원문·외부 입력·판단 문장은
넣지 않는다.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from ax_agent_template import common_log
from ax_agent_template.config import get_agent_name
from ax_contracts.data_access.common_log import LogRecord

_logger = logging.getLogger("ax.event")


def _source() -> str:
    try:
        return get_agent_name()
    except RuntimeError:
        return "unknown"


def log_event(
    context_id: str,
    tenant_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    source: str | None = None,
    skill: str | None = None,
    task_status: str | None = None,
    grade: str | None = None,
    requester: str | None = None,
) -> None:
    """사건 로그 한 줄을 남긴다. `kind`는 로그 종류(예: `request_received`)다.

    `source`는 이 줄을 기록한 에이전트(비우면 현재 에이전트 이름)이고, `requester`는 상대 쪽이다.
    """
    _logger.info(
        json.dumps(
            {"context_id": context_id, "tenant_id": tenant_id, "kind": kind, "payload": payload},
            ensure_ascii=False,
            default=str,
        )
    )
    if kind not in common_log.LOG_KINDS:
        return
    try:
        store = common_log.get_store()
        if store is None:
            return
        store.write(
            LogRecord.model_validate(
                {
                    "log_id": f"log_{uuid.uuid4().hex[:16]}",
                    "tenant_id": tenant_id,
                    "context_id": context_id,
                    "occurred_at": datetime.now(UTC),
                    "source": source or _source(),
                    "kind": kind,
                    "skill": skill,
                    "task_status": task_status,
                    "grade": grade,
                    "requester": requester,
                    "payload": payload,
                }
            )
        )
    except Exception:  # noqa: BLE001 - 로그 실패가 요청 처리를 막으면 안 된다
        _logger.warning("공통 로그 저장소에 쓰지 못했다: kind=%s context_id=%s", kind, context_id)
