"""A2A 요청·결과에 붙는 공통 필드 (공통 구성안 5장).

A2A SDK가 정한 프로토콜 필드는 SDK 그대로 쓰고, 우리가 정의하는 필드는 모두 snake_case다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class TaskStatus(StrEnum):
    """Task 상태. 값은 A2A 프로토콜 표기를 따른다(`input-required`).

    정책상 거절(예: 환불 신청을 정책에 따라 거절)은 요청을 정상 처리한 결과이므로
    `completed` + `outcome`으로 표현한다. `rejected`는 에이전트가 요청 자체를 거부한 경우
    (권한·형식·루프·토큰 위반)만 쓴다. 둘을 섞으면 이관률·실패율 지표가 오염된다.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELED = "canceled"
    FAILED = "failed"


class InputReason(StrEnum):
    """`input-required`일 때 무엇을 기다리는지 구분한다."""

    DEPT_INPUT = "dept_input"  # 다른 부서의 판단
    CUSTOMER_INPUT = "customer_input"  # 고객 자료
    HUMAN_INPUT = "human_input"  # 사람 확정·승인


class ErrorCode(StrEnum):
    """요청을 거부하거나 취소할 때 `outcome`에 담는 코드."""

    LOOP_DETECTED = "LOOP_DETECTED"
    HOP_LIMIT = "HOP_LIMIT"
    TIMEOUT = "TIMEOUT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN_SKILL = "FORBIDDEN_SKILL"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    SEQUENCE_GAP = "SEQUENCE_GAP"


class RequestMeta(AxModel):
    """모든 A2A 요청·결과에 붙는 메타.

    - `context_id`: 사건 단위 ID. 없으면 요청을 거부한다.
    - `path`: 거쳐온 에이전트 이름 목록. 새 요청에서 자기 이름이 있으면 루프로 거부하고,
      길이가 3을 넘으면 이관 큐로 넘긴다.
    - `deadline`: 응답 마감. 넘으면 실행하지 않고 보고한다.
    - `idempotency_key`: `context_id + 액션 + 대상`. 상태 변경 요청이면 필수다.
    - `requester`: 토큰에서 채워지는 값이다. 요청 본문에 적힌 값은 믿지 않고 무시한다.
    """

    context_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    token: str
    path: list[str] = Field(default_factory=list)
    deadline: UtcDatetime
    idempotency_key: str | None = None
    requester: str | None = None


class Evidence(AxModel):
    """결과에 붙는 근거. 근거가 없으면 "확인할 수 없음"으로 답한다."""

    queries: list[str] = Field(default_factory=list)
    data_summary: str = ""
    collected_at: UtcDatetime | None = None
    ref_ids: list[str] = Field(default_factory=list)


class AgentRequest(AxModel):
    """에이전트에 일을 맡기는 요청.

    - `untrusted`: 고객 문의·판매자 정보 같은 외부 입력. 프롬프트에는 데이터로만 넣고
      지시로 해석하지 않는다. 다른 에이전트에 넘길 때도 이 구획을 유지한다.
    - `sequence`: 순서가 중요한 요청(예: 보상 쿠폰)에서 쓴다. 번호가 건너뛰면 `SEQUENCE_GAP`.
    """

    meta: RequestMeta
    skill: str
    input: dict[str, Any] = Field(default_factory=dict)
    untrusted: dict[str, Any] = Field(default_factory=dict)
    sequence: int | None = None


class AgentResult(AxModel):
    """에이전트의 결과.

    `outcome` 관례:
    - 정책 거절: `auto_rejected` (status는 `completed`)
    - 재무 교차검증 거절: `rejected_by_review` (status는 `completed`)
    - 중복 연결: `linked_duplicate`
    - 요청 거부(`rejected`): `ErrorCode` 값
    - 마감 초과는 `canceled`(outcome `TIMEOUT`), 실행 오류는 `failed`

    `fallback`은 LLM·스키마 검증이 실패해 고정 템플릿으로 대체했을 때 true다.
    """

    meta: RequestMeta
    status: TaskStatus
    outcome: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence = Field(default_factory=Evidence)
    reason_code: InputReason | None = None
    decision_reason: str | None = None
    alternatives_considered: list[str] = Field(default_factory=list)
    fallback: bool = False

    @model_validator(mode="after")
    def _check_status_fields(self) -> AgentResult:
        if self.status == TaskStatus.INPUT_REQUIRED and self.reason_code is None:
            raise ValueError("input-required에는 reason_code가 필요하다")
        if self.status != TaskStatus.INPUT_REQUIRED and self.reason_code is not None:
            raise ValueError("reason_code는 input-required일 때만 쓴다")
        if self.status == TaskStatus.REJECTED:
            codes = {code.value for code in ErrorCode}
            if self.outcome not in codes:
                raise ValueError("rejected의 outcome은 ErrorCode 값이어야 한다")
        return self
