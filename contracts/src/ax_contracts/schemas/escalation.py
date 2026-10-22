"""이관 큐 항목. 이관은 통합 에이전트의 일상 업무가 아니라 설계 결함을 알리는 경보다.

이관 시 원문 대신 `context_id`, 경로, 사유만 큐에 넣고, 통합 에이전트가 비동기로 꺼내
규칙 기반으로 재분배한다. 이관 건수와 경로는 대시보드에 표시한다.
"""

from __future__ import annotations

from pydantic import Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel


class Escalation(AxModel):
    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    path: list[str] = Field(default_factory=list)
    reason: str
