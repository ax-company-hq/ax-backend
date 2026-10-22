"""hr(인사) 에이전트 skill 입출력 (인사 구성안 HR2, 2026-10-22 팀 공유·합의).

skill과 등급(등급 신고는 안전 레이어 레지스트리가 원본이다):

| skill | 등급 | 하는 일 |
| --- | --- | --- |
| `record_case` | A | 사건·결정 기록 → 결정 카드 (입력은 `RecordCase`, 출력은 `RecordCaseOutput`) |
| `start_handover` | A | 인수인계 초안·빈 자리·완성도, 빈 자리 질문 |
| `answer_handover_question` | A | 인계자의 답변을 받아 초안을 이어서 보완 |
| `confirm_handover` | B(검증자 planning), 완성도 미달이면 C | 인수인계 확정 |
| `request_assignment_change` | A | 교대·발령·퇴직·입사 요청 접수, 체크리스트·재배정 후보 |
| `confirm_reassignment` | B(코드 규칙 검증) | 담당 재배정 확정 |
| `confirm_offboarding` | C | 퇴직 확정(안전 레이어 보류 큐) |
| `start_onboarding` | A | 읽기 순서·입문 체크리스트 |
| `check_plan` | 읽기 | 새 계획이 과거에 기각된 안인지 판정 |
| `get_roster` | 읽기 | 담당·교대 명부 (개인정보 제외) |

인계자 질문은 `input-required`(+ `reason_code: human_input`) 결과의 `output`에 담는다.
질문을 기다리는 동안은 hr이 상태를 저장하고, 답변은 새 요청(`answer_handover_question`)으로
받는다. 요청마다 `deadline`은 그대로 짧게 둔다(사람 답변을 기다리며 요청을 열어 두지 않는다).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel, UtcDatetime

# ---- record_case ------------------------------------------------------------


class RecordCaseOutput(AxModel):
    """`record_case`의 결과.

    - `card_created`: 결정 카드를 만들어 저장했다.
    - `candidate`: 확신이 낮아 후보(`status=candidate`)로 두었다.
    - `record_only`: 결정이 없는 기록이라 기록만 남겼다.
    - `duplicate`: 같은 `context_id`·종류의 요청이 이미 처리되었다.
    """

    outcome: Literal["card_created", "candidate", "record_only", "duplicate"]
    card_id: str | None = None
    confidence: Literal["low", "mid", "high"] | None = None


# ---- 인수인계 ---------------------------------------------------------------

HandoverStatus = Literal["draft", "input_required", "ready", "confirmed", "held"]


class HandoverItem(AxModel):
    """인계 목록의 한 항목. `source_ref`는 planning 항목 ID나 결정 카드 ID다."""

    item_id: str
    kind: Literal["deployment", "ticket", "pr", "document", "decision", "other"]
    title: str
    source_ref: str | None = None


class HandoverQuestion(AxModel):
    """인계자에게 묻는 빈 자리 질문. 대시보드가 보여 주고 답을 받는다."""

    question_id: str
    context_id: str
    assignee_id: str  # 대상 인계자
    question: str
    gap_field: str  # 빈 자리 필드 이름


class StartHandoverInput(AxModel):
    owner_id: str  # 인계자 (`RosterEntry.member_id`)
    successor_id: str | None = None
    area: str | None = None
    reason: Literal["shift", "transfer", "offboarding", "other"] = "other"


class StartHandoverOutput(AxModel):
    """초안과 완성도. 완성도·빈 자리 수는 코드가 계산한다(LLM 값이 아니다)."""

    handover_id: str
    status: HandoverStatus
    items: list[HandoverItem] = Field(default_factory=list)
    completeness: int = Field(ge=0, le=100)
    gap_count: int = Field(ge=0)
    questions: list[HandoverQuestion] = Field(default_factory=list)


class AnswerHandoverQuestionInput(AxModel):
    question_id: str
    answer: str


class HandoverAnswerRequest(AxModel):
    """dashboard-api가 받는 답변 본문. 같은 답변을 두 번 보내도 한 번만 반영한다."""

    question_id: str
    answer: str
    idempotency_key: str


class AnswerHandoverQuestionOutput(AxModel):
    handover_id: str
    status: HandoverStatus
    completeness: int = Field(ge=0, le=100)
    remaining_questions: list[HandoverQuestion] = Field(default_factory=list)


class ConfirmHandoverInput(AxModel):
    handover_id: str


class ConfirmHandoverOutput(AxModel):
    """`confirmed`: 확정 / `held`: 완성도 미달로 보류 큐 / `rejected`: 검증자가 거절."""

    handover_id: str
    status: Literal["confirmed", "held", "rejected"]
    completeness: int = Field(ge=0, le=100)
    missing_item_ids: list[str] = Field(default_factory=list)
    hold_id: str | None = None


# ---- 교대·발령·퇴직·입사 ------------------------------------------------------


class ChecklistItem(AxModel):
    item_id: str
    title: str
    done: bool = False


class RequestAssignmentChangeInput(AxModel):
    change_kind: Literal["shift", "transfer", "offboarding", "join"]
    member_id: str
    area: str
    effective_at: UtcDatetime | None = None
    successor_id: str | None = None


class RequestAssignmentChangeOutput(AxModel):
    """권한·인계 목록은 전달만 한다. 회수는 각 시스템을 가진 부서가 실행한다."""

    change_id: str
    checklist: list[ChecklistItem] = Field(default_factory=list)
    successor_candidates: list[str] = Field(default_factory=list)
    access_list: list[str] = Field(default_factory=list)
    handover_id: str | None = None
    onboarding_id: str | None = None


class ConfirmReassignmentInput(AxModel):
    change_id: str
    successor_id: str


class ConfirmReassignmentOutput(AxModel):
    """코드 규칙 검증 결과. 담당 공백·중복이 있으면 `rejected`다."""

    change_id: str
    status: Literal["confirmed", "rejected"]
    gap_areas: list[str] = Field(default_factory=list)
    duplicate_areas: list[str] = Field(default_factory=list)


class ConfirmOffboardingInput(AxModel):
    change_id: str


class ConfirmOffboardingOutput(AxModel):
    """퇴직 확정은 C 등급이라 `held`(안전 레이어 보류 큐)로 시작한다."""

    change_id: str
    status: Literal["confirmed", "held"]
    hold_id: str | None = None


# ---- 온보딩 -----------------------------------------------------------------


class OnboardingStep(AxModel):
    step_id: str
    order: int = Field(ge=1)
    title: str
    card_id: str | None = None  # 읽을 결정 카드
    done: bool = False


class StartOnboardingInput(AxModel):
    member_id: str
    area: str
    handover_id: str | None = None


class StartOnboardingOutput(AxModel):
    onboarding_id: str
    steps: list[OnboardingStep] = Field(default_factory=list)
    progress: int = Field(ge=0, le=100)


# ---- 계획 비교 --------------------------------------------------------------

FindingCode = Literal["MATCH", "CONFLICT", "REJECTED_BEFORE"]


class PlanDraft(AxModel):
    title: str
    summary: str
    area: str  # `RosterEntry.area`와 같은 값 집합 (기획서 문단 id를 넣지 않는다)
    kind: Literal["spec", "ticket", "deploy_plan"]
    ref: str | None = None  # 호출 측 참조 id(예: 기획서 문단). 판정에는 쓰지 않는다


class CheckPlanInput(AxModel):
    """새 계획 초안. 초안 속 외부 문구는 `AgentRequest.untrusted`로 따로 보낸다."""

    draft: PlanDraft


class PlanFinding(AxModel):
    code: FindingCode
    level: Literal["info", "warn"]
    message: str
    card_ids: list[str] = Field(default_factory=list)  # 근거 카드


class SimilarCard(AxModel):
    card_id: str
    summary: str
    status: str


class CheckPlanComputed(AxModel):
    """숫자는 코드가 계산한다."""

    days_since_rejected: int | None = Field(default=None, ge=0)
    basis: str | None = None


class CheckPlanOutput(AxModel):
    """`MATCH`와 관련 카드 없음은 `ok`, 그 밖은 `warn`이다. 근거 카드가 없으면 판정하지 않는다."""

    verdict: Literal["ok", "warn"]
    findings: list[PlanFinding] = Field(default_factory=list)
    similar_cards: list[SimilarCard] = Field(default_factory=list)
    computed: CheckPlanComputed = Field(default_factory=CheckPlanComputed)
    no_related_cards: bool = False  # 관련 카드가 없으면 true ("기록에 없음")


# ---- 명부 -------------------------------------------------------------------


class RosterEntry(AxModel):
    """담당 영역과 역할만 담는다(개인 인사 정보 제외)."""

    area: str
    role: str
    member_id: str
    shift_start: UtcDatetime | None = None
    shift_end: UtcDatetime | None = None


class GetRosterInput(AxModel):
    area: str | None = None


class GetRosterOutput(AxModel):
    entries: list[RosterEntry] = Field(default_factory=list)
    gap_areas: list[str] = Field(default_factory=list)  # 담당 공백 영역
    collected_at: UtcDatetime
