"""지식 공백 보고서 skill의 입력·출력 모델. 숫자는 모두 코드가 문의·초안 기록에서 센다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel

GapType = Literal["no_document", "weak_document", "none", "insufficient_samples"]
SuggestedAction = Literal["add_document", "review_document", "none"]


class KnowledgeGapInput(AxModel):
    window_days: int = Field(default=7, ge=1, le=90)  # 시드
    min_samples: int = Field(default=5, ge=1)  # 이 건수 미만은 순위를 내지 않는다 (시드)
    category: str | None = None


class GapItem(AxModel):
    """분류(또는 분류+근거 문서) 한 묶음. 고객 원문은 담지 않고 문의 번호만 싣는다."""

    category: str
    doc_id: str | None = None  # 근거 문서별 줄일 때만 있다
    inquiries: int
    analyzed_messages: int  # 분모
    no_evidence: int  # 분석에 근거가 없던 고객 메시지 수
    drafts_decided: int  # 결정이 난 초안 수 (그대로·수정·거절·가드 차단)
    drafts_edited: int
    drafts_rejected: int
    drafts_blocked: int
    escalated: int
    reject_reasons: dict[str, int] = Field(default_factory=dict)
    no_evidence_rate: float | None = None  # 분모가 0이면 None
    reject_rate: float | None = None  # (거절 + 가드 차단) / 결정한 초안
    escalated_rate: float | None = None
    gap_type: GapType
    gap_score: float | None = None  # 위 세 비율의 단순 평균 (가중치 1:1:1은 시드)
    rank: int | None = None  # 표본이 부족하면 None
    sample_inquiry_ids: list[int] = Field(default_factory=list)
    owner_target: str  # 처방을 받을 곳의 코드 (부서 이름이 아님)
    suggested_action: SuggestedAction


class KnowledgeGapReport(AxModel):
    window_days: int
    min_samples: int
    evaluated_at: str
    categories: list[GapItem]  # 순위 순, 표본 부족은 뒤
    documents: list[GapItem]  # 근거가 있던 메시지만 문서별로
    notes: list[str] = Field(default_factory=list)
