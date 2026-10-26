"""보류(C) 건을 다른 에이전트가 검토하는 skill의 공통 입출력 (#191). 인터페이스만 둔다.

검토자마다 같은 형식을 쓴다. 판정은 검토자가 자기 원천으로 하고, 요청 쪽 `evidence`는 참고용이다.
`requester`와 검토자가 같으면 안전 레이어가 거부한다. 구현은 각 검토자 담당이 한다.
학습 기준(C → B) 모델은 팀 결정 뒤에 추가한다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from ax_contracts.schemas.common import AxModel
from ax_contracts.schemas.execute import HoldReason
from ax_contracts.schemas.message import Evidence


class ActionReviewInput(AxModel):
    """검토자에게 넘기는 보류 건.

    - `execution_id`: 안전 레이어의 보류 건
    - `requester`: 일을 한 에이전트
    - `params`: `execute()`에 보낸 params 그대로.
      요청 쪽 데이터(`summary` 등)라 판정 근거로 쓰지 않는다
    - `hold_reason`: 보류 사유 하나 (`HoldReason`)
    - `rule_keys`: 보류 기록 `rules`에 걸린 규칙 키 (`spec_open_question`, `base_grade` 등)
    """

    execution_id: str
    action: str
    requester: str
    params: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence = Field(default_factory=Evidence)
    hold_reason: HoldReason
    rule_keys: list[str] = Field(default_factory=list)


class ActionReviewOutput(AxModel):
    """검토 결과. `reason_text`는 코드가 만든 값만 담는다 (LLM 자유 문장 금지)."""

    decision: Literal["approve", "reject", "escalate"]
    reason_code: str
    reason_text: str
    evidence: Evidence = Field(default_factory=Evidence)
