"""이관 큐 항목. 이관은 통합 에이전트의 일상 업무가 아니라 설계 결함을 알리는 경보다.

이관 시 원문 대신 `context_id`, 경로, 사유만 큐에 넣고, 통합 에이전트가 비동기로 꺼내
규칙 기반으로 재분배한다. 이관 건수와 경로는 대시보드에 표시한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class Escalation(AxModel):
    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    path: list[str] = Field(default_factory=list)
    reason: str


# ---- 진행 중 사건 조회 (#258 결정 9번 ①, #294)
#
# - `find_active_incident`: support 등 → orchestrator, 등급 read.
#   고객 문의·보상 요청이 장애 때문이면 그 사건의 `context_id`(`ctx_inc_`)를 이어받으려고 묻는다.
#   그래야 시나리오 A의 모든 Task가 사건 contextId 하나로 이어지고,
#   재무 손실 리포트가 보상 쿠폰을 사건 contextId로 셀 수 있다.
# - 판정은 총괄이 코드로 한다: 같은 `component`의 열린 사건, 없으면 `occurred_at`이
#   사건 구간(열림~닫힘) 안에 드는 최근 사건.
#   맞는 사건이 없으면 `found=False`로 답하고 지어내지 않는다.
# - 사건 번호(`ctx_inc_`)는 총괄만 발급한다. 묻는 쪽은 받은 값을 그대로 쓰고 새로 만들지 않는다.


class FindActiveIncidentInput(AxModel):
    """`component`는 `payment`·`api` 같은 구성요소 코드, `occurred_at`은 문의·주문 시각이다."""

    component: str | None = None
    occurred_at: UtcDatetime | None = None


class FindActiveIncidentOutput(AxModel):
    """맞는 사건이 없으면 `found=False`이고 나머지는 비운다."""

    found: bool
    context_id: str | None = None  # ctx_inc_…
    component: str | None = None
    status: Literal["open", "resolved", "degraded"] | None = None
    opened_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    # 점검 공지가 게시됐으면 마케팅 공지 번호(`ntc_…`), 아직 없으면 None.
    # 총괄이 사건에 보관한 값이다. 고객지원이 `get_notice`로 공지 사실을 읽을 때 쓰며,
    # 지어내지 않는다.
    notice_id: str | None = None

    @model_validator(mode="after")
    def _found_has_context(self) -> FindActiveIncidentOutput:
        if self.found and not self.context_id:
            raise ValueError("found=True면 context_id가 필요하다")
        if not self.found and (self.context_id or self.status or self.opened_at or self.notice_id):
            raise ValueError("found=False면 사건 필드를 비운다")
        return self
