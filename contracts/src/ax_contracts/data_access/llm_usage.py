"""LLM 호출 1회의 사용량 기록과 읽기 조회 형식 (NFR-06).

쓰기는 agent-template의 LLM 래퍼만 한다. 읽기는 이 형식의 읽기 전용 조회만 쓴다
(dashboard-api, data). 프롬프트·고객 원문은 담지 않고, 부서·모델 이름과 숫자만 담는다.
공통 로그의 `LogKind`는 응답 enum이라 값을 늘리지 않고 이 형식을 따로 둔다.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from ax_contracts.schemas.common import TENANT_ID_DEFAULT, AxModel, UtcDatetime


class LlmCallRecord(AxModel):
    """LLM 호출 1회. `context_id`는 사건과 무관한 호출이면 None이다."""

    log_id: str
    tenant_id: str = TENANT_ID_DEFAULT
    context_id: str | None = None
    occurred_at: UtcDatetime
    source: str  # 호출한 에이전트 이름
    department: str
    model: str | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    success: bool


class LlmUsageQuery(AxModel):
    """입력. `from`은 파이썬 예약어라 별칭으로 받는다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    context_ids: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    from_: UtcDatetime | None = Field(default=None, alias="from")
    to: UtcDatetime | None = None
    limit: int = Field(default=1000, ge=1, le=5000)
    tenant_id: str = TENANT_ID_DEFAULT


class LlmUsageResult(AxModel):
    """출력. `records`는 occurred_at 오름차순이다. limit에 걸려 잘렸으면 `truncated`가 True다."""

    records: list[LlmCallRecord] = Field(default_factory=list)
    truncated: bool = False
