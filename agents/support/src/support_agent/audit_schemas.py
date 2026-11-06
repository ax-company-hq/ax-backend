"""개인정보 감사 기록과 `get_privacy_audit` skill의 입력·출력 모델.

기록에는 원문, 마스킹한 글, 값의 해시를 넣지 않는다. 종류·건수·ID·코드·시각만 담는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel

PII_KINDS = ("phone", "email", "rrn", "card", "account")
Stage = Literal["ingest", "llm_input", "event_untrusted", "transfer"]
# none: 탐지된 개인정보가 없었다 (설계의 masked·blocked·dropped에 더한 값)
MaskingAction = Literal["none", "masked", "blocked", "dropped"]
ReplyPath = Literal["llm", "template_fallback", "rules"]
ReplyOutcome = Literal["generated", "accepted", "edited", "rejected", "blocked"]
RecordType = Literal["masking", "provenance"]


class MaskingAuditRecord(AxModel):
    """어느 단계에서 개인정보 몇 건을 어떻게 다뤘는지. 탐지한 것의 증빙이지 부재 증빙은 아니다."""

    audit_id: int
    record_type: Literal["masking"] = "masking"
    tenant_id: str
    context_id: str
    inquiry_id: int | None = None
    message_id: int | None = None
    seq: int | None = None  # 문의 안 메시지 순번 (1부터)
    stage: Stage
    detected: dict[str, int]  # 종류별 건수. 규칙이 못 가리는 이름·주소·일반전화는 나오지 않는다
    action: MaskingAction
    reason_code: str | None = None  # 막았다면 그 코드 (예: secure_transfer_plain_pii)
    rules_version: str  # 마스킹 규칙(정규식 목록)의 버전
    occurred_at: str


class SourceRef(AxModel):
    id: str
    score: float


class ReplyProvenance(AxModel):
    """초안이 어떤 근거·경로·모델로 만들어졌고 어떻게 결정됐는지. 문장은 담지 않는다."""

    audit_id: int
    record_type: Literal["provenance"] = "provenance"
    tenant_id: str
    context_id: str
    inquiry_id: int | None = None
    message_id: int | None = None
    draft_style: str
    source_ids: list[SourceRef] = Field(default_factory=list)
    path: ReplyPath
    model_name: str | None = None  # `SUPPORT_LLM_MODEL` 값. 키는 담지 않는다
    guard_codes: list[str] = Field(default_factory=list)
    outcome: ReplyOutcome
    actor: Literal["agent", "human"]
    occurred_at: str


class GetPrivacyAuditInput(AxModel):
    inquiry_id: int
    kinds: list[RecordType] | None = None  # 비우면 둘 다


class ProcessingActivity(AxModel):
    """코드 상수에서 만든 처리 활동 요약. 법적 근거나 적법성은 적지 않는다."""

    categories: list[str]
    purposes: list[str]
    recipients: list[str]
    retention: str
    cross_border: str


class PrivacyAudit(AxModel):
    inquiry_id: int
    masking: list[MaskingAuditRecord] = Field(default_factory=list)
    provenance: list[ReplyProvenance] = Field(default_factory=list)
    rules_version: str
    processing_activity: ProcessingActivity
    notes: list[str] = Field(default_factory=list)
