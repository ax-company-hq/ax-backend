"""개인정보 삭제 요청 1차 판단 skill의 입출력 (#430). 읽기이고 삭제·본인 확정을 하지 않는다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel

Verdict = Literal["consistent", "mismatch", "insufficient"]
Confidence = Literal["low", "medium", "high"]  # 퍼센트를 쓰지 않는다
Recommendation = Literal["review", "reject_recommended"]


class AssessDeletionInput(AxModel):
    """판단할 요청은 문의에 붙은 삭제 요청이다. 다른 주문이나 고객은 받지 않는다."""

    inquiry_id: int


class DeletionEvidence(AxModel):
    """근거 한 줄. `matched`는 true=문제 없음, false=어긋남, null=확인할 수 없음.

    `note`는 코드가 정한 고정 문장이다. 이름·연락처·주문 내용 같은 개인정보는 담지 않는다.
    """

    kind: str
    matched: bool | None = None
    note: str


class DeletionAssessment(AxModel):
    """참고 의견이다. 본인으로 확정하거나 삭제를 허락하지 않고, 최종 판단은 사람이 한다.

    `summary`는 사람이 보류 큐에서 읽을 문장이다 (코드가 만든다, 고객 원문이 아니다).
    """

    inquiry_id: int
    buyer_id: str | None = None  # 요청한 쪽의 가명 구매자 ID (buy_…)
    verdict: Verdict
    confidence: Confidence
    recommendation: Recommendation
    summary: str
    evidence: list[DeletionEvidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
