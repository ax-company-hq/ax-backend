"""인사 `record_case`와 회사 기억 카드 (공통 구성안 8장).

hr은 사건·결정을 기록(`RecordCase`)으로 받아 카드(`Card`)를 만들고, 카드의 저장·검색은
회사 기억(memory)이 맡는다. 카드 문장과 기각 판정은 hr 한 곳에서만 만든다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class RecordKind(StrEnum):
    """`record_case`로 보내는 기록의 종류.

    `rollback_executed`는 infra가, `spec_confirmed`·`pr_merged`·`decision_extracted`·
    `request_closed`·`decision_rejected`는 planning이 보낸다. `decision_rejected`(기각한 안)는
    hr `check_plan`이 "과거에 기각된 안"을 판정하는 재료라서 기각 이유와 검토한 대안을 함께 보낸다.
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
    DECISION_REJECTED = "decision_rejected"


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
    area: str | None = None  # 담당 영역(`RosterEntry.area`와 같은 값 집합). 없으면 영역 미지정


class Card(AxModel):
    """회사 기억에 저장하는 카드.

    - `status`: 확신이 낮아 사람 확인을 기다리는 카드는 `candidate`, 사람이 승인하면 `active`다.
      새 결정이 기존 결정을 바꾸면 기존 카드는 `superseded`가 되고
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
    status: Literal["active", "candidate", "superseded", "retired"] = "active"
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


CardKind = Literal["incident", "decision", "case", "notice", "policy", "fraud"]
CardId = Annotated[str, StringConstraints(pattern=r"^card_")]


class CardDraft(AxModel):
    """카드 쓰기 입력. `Card`에서 memory가 정하는 `card_id`·`created_at`·`status`·
    `superseded_by`를 뺀 형태다."""

    kind: CardKind
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    author: str
    confidence: Literal["low", "mid", "high"]
    extra: dict[str, Any] = Field(default_factory=dict)


class CardWriteInput(AxModel):
    """`memory_write_card` 입력.

    멱등 키는 `context_id + kind + 제목 해시`다. 같은 키의 카드가 이미 있으면
    새로 만들지 않고 기존 `card_id`를 돌려준다.
    """

    card: CardDraft


class CardWriteOutput(AxModel):
    """`memory_write_card` 출력. 같은 멱등 키로 이미 있으면 `created`가 false다."""

    card_id: CardId
    created: bool


class CardSearchInput(AxModel):
    """`memory_search_card` 입력."""

    query: str
    kind: list[CardKind] = Field(default_factory=list)
    status: Literal["active", "superseded", "retired"] = "active"
    since: UtcDatetime | None = None
    top_k: int = Field(default=5, ge=1, le=20)


class CardHit(AxModel):
    """검색 결과 한 건. `low_confidence`는 카드 confidence가 low일 때 true이며
    판정 근거로 쓰지 않는다는 표시다."""

    card: Card
    score: float = Field(ge=0, le=1)
    low_confidence: bool


class CardSearchOutput(AxModel):
    hits: list[CardHit] = Field(default_factory=list)


class CardGetInput(AxModel):
    """`memory_get_card` 입력."""

    card_id: CardId


class CardGetOutput(AxModel):
    card: Card


class CardLinkInput(AxModel):
    """`memory_link_card` 입력. 옛 카드를 새 카드로 대체하거나 폐기로 연결한다."""

    old_card_id: CardId
    new_card_id: CardId
    relation: Literal["superseded", "retired"]


class CardLinkOutput(AxModel):
    old_card: Card
