"""Agent Card — 각 에이전트가 `/.well-known/agent-card.json`으로 공개하는 명함.

다른 에이전트와 통합 에이전트는 이 목록을 읽고 일을 분배하고, 실행 관문은 `allowed_requesters`로
권한(ACL)을 확인한다. 실제 등급은 안전 레이어의 레지스트리가 우선한다(여기에는 기본 등급을 적는다).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ax_contracts.schemas.common import AxModel

SNAKE_CASE = r"^[a-z][a-z0-9_]*$"

Grade = Literal["read", "A", "B", "C"]
Priority = Literal["required", "recommended", "optional"]


class Capabilities(AxModel):
    streaming: bool = True
    push_notifications: bool = False


class Skill(AxModel):
    """다른 에이전트가 요청할 수 있는 일 하나.

    - `grade`: `read`는 상태 변경 없음. 그 밖의 등급은 실행할 때 반드시 `execute()`를 거친다.
    - `verifier`: B 등급의 교차검증 주체. B면 필수이고 요청 부서와 같으면 안 된다.
    - `input_schema`·`output_schema`: 스키마 파일 경로 문자열.
    """

    id: str = Field(pattern=SNAKE_CASE)
    description: str
    input_schema: str
    output_schema: str
    grade: Grade
    allowed_requesters: list[str]
    verifier: str | None = None
    priority: Priority

    @model_validator(mode="after")
    def _b_grade_needs_verifier(self) -> Skill:
        if self.grade == "B" and not self.verifier:
            raise ValueError(f"skill {self.id}: grade B에는 verifier가 필요하다")
        return self


class AgentCard(AxModel):
    """에이전트 명함. 한 에이전트 = 카드 한 장."""

    name: str = Field(pattern=SNAKE_CASE)
    owner: str
    description: str
    url: str
    version: str = "0.1.0"
    capabilities: Capabilities = Field(default_factory=Capabilities)
    skills: list[Skill]

    @model_validator(mode="after")
    def _no_self_verification(self) -> AgentCard:
        """자기 검증 금지: verifier가 이 에이전트 자신이면 거부한다."""
        for skill in self.skills:
            if skill.verifier == self.name:
                raise ValueError(
                    f"skill {skill.id}: verifier가 자기 에이전트({self.name})일 수 없다"
                    " (자기 검증 금지)"
                )
        ids = [skill.id for skill in self.skills]
        if len(ids) != len(set(ids)):
            raise ValueError("skill id가 중복되었다")
        return self
