"""planning 에이전트 skill 입출력 (시나리오 A 필수 3개). 이슈 #10 요청(예진).

- `find_related_prs`: infra·data·orchestrator → planning, 등급 read.
  후보만 돌려주고 원인은 판정하지 않는다.
- `link_incident`: orchestrator·infra → planning, 등급 A. 사건에 PR·기획 문단을 연결한다.
- `verify_rollback_target`: safety → planning. infra `rollback_deployment`(B)의 교차검증이다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

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
#
# 값의 의미 (planning·hr이 같은 값을 쓴다):
# - `owner_id`: 명부의 `RosterEntry.member_id`와 같은 값이다(GitHub 로그인·부서 id가 아니다).
#   planning은 GitHub 로그인과의 대응을 자기 쪽에서 `member_id`로 바꿔 거른다.
# - `area`: 명부 `RosterEntry.area`와 같은 값 집합이다(변경 경로 접두사가 아니다).
#   planning이 작업·PR을 영역에 대응시키는 방법(기획서 문단·변경 경로)은 planning 내부 구현이다.
# - `kind="deployment"`: planning은 배포를 직접 하지 않고 머지된 PR만 안다. 그래서
#   "dev에 머지됐지만 복구·확인이 끝나지 않은 PR"을 돌려준다. 배포 상태 자체는 infra 소관이라
#   이 skill이 돌려주지 않는다.


class InProgressItem(AxModel):
    item_id: str
    kind: Literal["deployment", "ticket", "pr"]
    title: str
    status: str
    owner_id: str  # `RosterEntry.member_id`
    area: str | None = None  # `RosterEntry.area`
    updated_at: UtcDatetime | None = None


class ListInProgressInput(AxModel):
    owner_id: str  # `RosterEntry.member_id`
    area: str | None = None  # `RosterEntry.area`


class ListInProgressOutput(AxModel):
    items: list[InProgressItem] = Field(default_factory=list)
    collected_at: UtcDatetime  # 조회 시각 (근거)


class VerifyHandoverInput(AxModel):
    handover_id: str
    owner_id: str  # `RosterEntry.member_id`
    area: str | None = None  # `RosterEntry.area`
    handover_item_ids: list[str] = Field(default_factory=list)  # 인계 목록에 적힌 항목 ID


class VerifyHandoverOutput(AxModel):
    """`approved` · `reason_code="complete"` · `missing=[]`는 서로 같은 뜻이다."""

    approved: bool
    reason_code: Literal["complete", "missing_items"]
    missing: list[InProgressItem] = Field(default_factory=list)  # 인계 목록에 빠진 진행 중 항목
    # 인계 목록에는 있지만 이미 끝난 항목의 ID. 정보로만 돌려주고 판정에는 쓰지 않는다.
    stale_item_ids: list[str] = Field(default_factory=list)
    checked_at: UtcDatetime

    @model_validator(mode="after")
    def _verdict_is_consistent(self) -> VerifyHandoverOutput:
        complete = self.reason_code == "complete"
        if self.approved != complete or complete != (not self.missing):
            raise ValueError("approved, reason_code, missing이 서로 맞지 않는다")
        return self


# ---- 변경 요청 접수 (이슈 #172 요청, planning)
#
# - `create_ticket`: support·marketing·hr·data·orchestrator → planning, 등급 A.
#   반복 문의 같은 근거로 기획개발에 변경 요청을 접수한다.
# - `ctx_req_`는 planning이 발급한다. 같은 요청을 여러 부서가 보내면 하나로 묶어야 해서다.
#   요청 부서는 돌려받은 `context_id`로 자기 쪽 문의와 요청을 연결한다.
# - 결과 통지(머지 뒤 요청 부서 회신)는 원래 요청의 meta를 재사용하지 않고, 같은 `context_id`에
#   `path`를 비운 새 요청으로 보낸다(LOOP_DETECTED 방지).
# - `untrusted`: 고객 문의 원문은 넣지 않고 `evidence` 요약만 보낸다(필요하면 `untrusted.text`).


class CreateTicketInput(AxModel):
    title: str  # 바꿀 내용 한 줄
    evidence: str  # 근거 요약 (예: 반복 문의 42건)
    source_ref: str | None = None  # 요청 부서 쪽 참조 ID (예: 고객지원 문의 ID). 판정에 안 쓴다
    evidence_ids: list[str] = Field(default_factory=list)  # 근거 ID


class CreateTicketOutput(AxModel):
    request_id: str  # REQ-n
    context_id: str  # ctx_req_ (planning이 발급)
    outcome: Literal["created", "linked_duplicate"]
    duplicate_of: str | None = None  # 같은 요청이 이미 있으면 그 REQ ID

    @model_validator(mode="after")
    def _duplicate_is_consistent(self) -> CreateTicketOutput:
        if (self.outcome == "linked_duplicate") != (self.duplicate_of is not None):
            raise ValueError("duplicate_of는 outcome이 linked_duplicate일 때만 채운다")
        return self
