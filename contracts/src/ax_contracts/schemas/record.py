"""인사 `record_case`와 회사 기억 카드 (공통 구성안 8장).

hr은 사건·결정을 기록(`RecordCase`)으로 받아 카드(`Card`)를 만들고, 카드의 저장·검색은
회사 기억(memory)이 맡는다. 카드 문장과 기각 판정은 hr 한 곳에서만 만든다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class RecordKind(StrEnum):
    """`record_case`로 보내는 기록의 종류.

    `rollback_executed`는 infra가, `spec_confirmed`·`pr_merged`·`decision_extracted`·
    `request_closed`는 planning이 보낸다.
    """

    INCIDENT = "incident"
    DECISION = "decision"
    CASE = "case"
    NOTICE = "notice"
    ROLLBACK_EXECUTED = "rollback_executed"
    SPEC_CONFIRMED = "spec_confirmed"
    PR_MERGED = "pr_merged"
    DECISION_EXTRACTED = "decision_extracted"
    REQUEST_CLOSED = "request_closed"


class RecordCase(AxModel):
    """hr `record_case`의 입력. 결정 이유와 검토한 대안을 채우면 기록 정확도가 올라간다."""

    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    kind: RecordKind
    summary: str
    decision_reason: str | None = None
    alternatives_considered: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    author: str


class Card(AxModel):
    """회사 기억에 저장하는 카드.

    - `status`: 새 결정이 기존 결정을 바꾸면 기존 카드는 `superseded`가 되고
      `superseded_by`에 새 카드 ID를 적는다.
    - `confidence`: 자동 추출의 확신도. 낮으면 확정하지 않고 후보로 둔다.
    """

    card_id: str
    kind: Literal["incident", "decision", "case", "notice", "policy", "fraud"]
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    status: Literal["active", "superseded", "retired"] = "active"
    superseded_by: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    author: str
    confidence: Literal["low", "mid", "high"]
    created_at: UtcDatetime
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _superseded_by_matches_status(self) -> Card:
        if self.status == "superseded" and not self.superseded_by:
            raise ValueError("superseded 카드에는 superseded_by가 필요하다")
        if self.status != "superseded" and self.superseded_by is not None:
            raise ValueError("superseded_by는 superseded 카드에서만 쓴다")
        return self
