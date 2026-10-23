"""기획개발 변경 요청 skill의 입출력. 기획개발 쪽 모델은 계약(`CreateTicketInput/Output`)을 쓴다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel
from support_agent.change_requests import ChangeRequestLink, ChangeStatus


class RequestChangeInput(AxModel):
    """문의에서 나온 변경 요청. 고객 원문은 받지 않는다 (제목·근거는 상담원이나 코드가 쓴 요약).

    `evidence`는 건수·ID 같은 사실 요약이다 (예: `반복 문의 42건`). 전화번호 등 개인정보가 있으면
    거부한다.
    """

    inquiry_id: int
    title: str = Field(min_length=1, max_length=200)  # 바꿀 내용 한 줄
    evidence: str = Field(min_length=1, max_length=500)  # 근거 요약
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class RequestChangeOutput(AxModel):
    """`created`: 새 요청 / `linked_duplicate`: 같은 요청이 있어 거기에 묶였다."""

    outcome: Literal["created", "linked_duplicate"]
    request_id: str  # REQ-n (기획개발)
    context_id: str  # 기획개발이 발급한 ctx_req_
    duplicate_of: str | None = None
    link: ChangeRequestLink


class ListChangeRequestsInput(AxModel):
    inquiry_id: int | None = None
    context_id: str | None = None
    status: ChangeStatus | None = None


class ListChangeRequestsOutput(AxModel):
    items: list[ChangeRequestLink] = Field(default_factory=list)
