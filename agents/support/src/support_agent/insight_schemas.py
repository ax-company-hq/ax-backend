"""문의 집계·급증 감지 skill의 입력·출력 모델. 숫자는 모두 코드가 문의 기록에서 센다."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel


class KeywordCount(AxModel):
    keyword: str
    count: int  # 이 키워드가 나온 고객 메시지 수


class InquiryKpis(AxModel):
    """상담원 화면 KPI. 비율은 분모가 0이면 None이다 (0으로 채우지 않는다)."""

    conversations: int
    by_status: dict[str, int]
    agent_replies: int
    draft_adoption_rate: float | None = None  # (그대로 + 수정해서 쓴 것) / 결정한 초안 수
    accepted_as_is: int
    edited: int
    rejected: int
    avg_edit_ratio: float | None = None  # 수정해서 보낸 답변이 초안과 다른 정도 (0~1)
    avg_first_draft_ms: int | None = None
    drafts_generated: int
    drafts_blocked_by_guard: int
    guard_findings: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    by_urgency: dict[str, int] = Field(default_factory=dict)
    by_sentiment: dict[str, int] = Field(default_factory=dict)
    by_language: dict[str, int] = Field(default_factory=dict)  # 문의별 마지막 고객 메시지 언어
    top_keywords: list[KeywordCount] = Field(default_factory=list)  # 많이 나온 순, 최대 10개


class WaitingInquiry(AxModel):
    id: int
    customer_name: str
    subject: str
    category: str | None = None
    urgency: str | None = None
    sentiment: str | None = None
    preview: str
    waiting_minutes: int


class InquiryOverview(AxModel):
    kpis: InquiryKpis
    waiting: list[WaitingInquiry]  # 답변을 기다리는 문의, 긴급도·오래 기다린 순


BaselineMethod = Literal["mean", "median"]


class SurgeInput(AxModel):
    window_minutes: int = Field(default=60, ge=1, le=1440)
    baseline_hours: int = Field(default=24, ge=1, le=168)
    min_count: int = Field(default=5, ge=1)  # 이 건수 미만은 급증으로 보지 않는다
    ratio: float = Field(default=3.0, gt=1.0)  # 평소 대비 배수
    # 평소를 정하는 방식: mean=기준 기간의 창당 평균, median=창별 건수의 중앙값 (이상치에 덜 흔들림)
    baseline_method: BaselineMethod = "mean"


class TopicSurge(AxModel):
    """분류(topic_code)별 급증. 샘플은 문의 ID만 담고 원문은 담지 않는다."""

    topic_code: str
    count: int
    baseline_per_window: float
    ratio: float | None  # 평소가 0이면 None (배수를 정할 수 없다)
    sample_inquiry_ids: list[int]


class SurgeReport(AxModel):
    window_minutes: int
    baseline_hours: int
    baseline_method: BaselineMethod = "mean"
    evaluated_at: str
    total_in_window: int
    surges: list[TopicSurge]
