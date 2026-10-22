"""planning 에이전트 skill 입출력 (시나리오 A 필수 3개). 이슈 #10 요청(예진).

- `find_related_prs`: infra·data·orchestrator → planning, 등급 read.
  후보만 돌려주고 원인은 판정하지 않는다.
- `link_incident`: orchestrator·infra → planning, 등급 A. 사건에 PR·기획 문단을 연결한다.
- `verify_rollback_target`: safety → planning. infra `rollback_deployment`(B)의 교차검증이다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel, UtcDatetime


class RelatedPr(AxModel):
    pr_number: int
    title: str
    merged_at: UtcDatetime
    spec_id: str | None = None  # 근거 기획 문서
    para_id: str | None = None  # 요구사항·완료 기준 문단 id (R1, C2 …)
    changed_paths: list[str] = Field(default_factory=list)


class FindRelatedPrsInput(AxModel):
    window_from: UtcDatetime
    window_to: UtcDatetime
    component: str | None = None


class FindRelatedPrsOutput(AxModel):
    candidates: list[RelatedPr] = Field(default_factory=list)


class LinkIncidentInput(AxModel):
    incident_context_id: str
    pr_numbers: list[int]
    follow_up_title: str | None = None  # 있으면 재발 방지 요청을 등록한다


class LinkIncidentOutput(AxModel):
    linked_paras: list[str] = Field(default_factory=list)
    # 같은 요청이 이미 있으면 그 ID (outcome은 linked_duplicate)
    follow_up_request_id: str | None = None


class VerifyRollbackTargetInput(AxModel):
    service: str
    target_version: str
    deployed_at: UtcDatetime
    incident_opened_at: UtcDatetime


class VerifyRollbackTargetOutput(AxModel):
    approved: bool
    reason_code: Literal["deploy_before_incident", "no_deploy_in_window", "deploy_after_incident"]
    related_prs: list[RelatedPr] = Field(default_factory=list)


# ---- hr 연동 (이슈 #10 이후, 인사 구성안 HR2 연동 ④·인수인계 검증)
#
# - `list_in_progress`: hr → planning, 읽기. 담당자·영역의 진행 중 배포·미완료 티켓·열린 PR을
#   돌려준다(인수인계 초안의 "진행 중 항목"). 공지된 제약은 1차에서 뺐다.
# - `verify_handover`: hr `confirm_handover`(B)의 검증 skill. 인계 목록에 진행 중 항목이
#   빠짐없이 있는지 사실만 확인한다(내용 판단은 하지 않는다).


class InProgressItem(AxModel):
    item_id: str
    kind: Literal["deployment", "ticket", "pr"]
    title: str
    status: str
    owner_id: str
    area: str | None = None
    updated_at: UtcDatetime | None = None


class ListInProgressInput(AxModel):
    owner_id: str
    area: str | None = None


class ListInProgressOutput(AxModel):
    items: list[InProgressItem] = Field(default_factory=list)
    collected_at: UtcDatetime  # 조회 시각 (근거)


class VerifyHandoverInput(AxModel):
    handover_id: str
    owner_id: str
    area: str | None = None
    handover_item_ids: list[str] = Field(default_factory=list)  # 인계 목록에 적힌 항목 ID


class VerifyHandoverOutput(AxModel):
    approved: bool
    reason_code: Literal["complete", "missing_items"]
    missing: list[InProgressItem] = Field(default_factory=list)  # 인계 목록에 빠진 진행 중 항목
    checked_at: UtcDatetime
