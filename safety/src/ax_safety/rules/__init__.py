"""액션 등급 레지스트리와 기준 금액 등급표 (SAF-02).

설정 파일(`registry.toml`, `thresholds.toml`)은 사람만 바꾼다. 여기는 읽고 검증만 한다.
실행 중 숫자 기준 바꾸기는 `gate.Gate.request_threshold_change`가 메모리에서 한다.
`SAFETY_REGISTRY_PATH`·`SAFETY_THRESHOLDS_PATH`로 다른 파일을 쓸 수 있다(테스트용).
"""

from __future__ import annotations

import os
import tomllib
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Grade = Literal["A", "B", "C"]
GRADE_ORDER: dict[str, int] = {"A": 0, "B": 1, "C": 2}

# 검증자가 이 값이면 에이전트가 아니라 안전 레이어의 코드 규칙(기준표)이 B를 검증한다
RULES_VERIFIER = "safety"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AmountSpec(_Strict):
    """금액 계산 방법. `count_field × unit_field` 또는 `amount_field` 하나만 쓴다."""

    count_field: str | None = None
    unit_field: str | None = None
    amount_field: str | None = None
    claimed_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_way(self) -> AmountSpec:
        product = self.count_field is not None and self.unit_field is not None
        single = self.amount_field is not None
        if product == single:
            raise ValueError("amount는 count_field+unit_field 또는 amount_field 중 하나만 쓴다")
        return self


class ActionSpec(_Strict):
    """`requesters`가 비어 있으면 `owner`만 요청할 수 있다."""

    action: str
    owner: str
    requesters: list[str] = Field(default_factory=list)
    base_grade: Grade
    verifier: str | None = None
    verify_skill: str | None = None
    amount: AmountSpec | None = None
    customer_field: str | None = None
    purpose_field: str | None = None

    @property
    def allowed_requesters(self) -> list[str]:
        return self.requesters or [self.owner]

    @property
    def rules_verified(self) -> bool:
        return self.verifier == RULES_VERIFIER

    @model_validator(mode="after")
    def _verifier_rules(self) -> ActionSpec:
        if self.base_grade == "B" and not self.verifier:
            raise ValueError(f"{self.action}: B 등급에는 verifier가 필요하다")
        if self.verifier and not self.rules_verified and not self.verify_skill:
            raise ValueError(f"{self.action}: 에이전트 검증자에는 verify_skill이 필요하다")
        if self.verifier is not None and self.verifier in self.allowed_requesters:
            raise ValueError(f"{self.action}: 자기 검증 금지 (verifier가 요청자에 있다)")
        return self


class Rule(_Strict):
    """금액 규칙과 금액이 아닌 규칙.

    - 금액: `amount`·`unit`·`daily_total`·`context_total`·`customer_total` (`op`·`value`)
    - 숫자 파라미터: `param` + `field` (`op`·`value`), 예: `pr_changed_lines`
    - 경로 목록: `paths_within` + `field` + `allowed` (하나라도 허용 접두사 밖이면 걸림)
    - 보호 경로: `paths_avoid` + `field` + `protected` (하나라도 보호 경로와 같거나 그 아래면 걸림)
    """

    key: str
    action: str
    metric: Literal[
        "amount",
        "unit",
        "daily_total",
        "context_total",
        "customer_total",
        "param",
        "paths_within",
        "paths_avoid",
    ]
    op: Literal[">=", ">", "<", "<="] = ">="
    value: int = Field(default=0, ge=0)
    field: str | None = None
    allowed: list[str] = Field(default_factory=list)
    protected: list[str] = Field(default_factory=list)
    when: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _field_needed(self) -> Rule:
        if self.metric in ("param", "paths_within", "paths_avoid") and not self.field:
            raise ValueError(f"{self.key}: {self.metric} 규칙에는 field가 필요하다")
        if self.metric == "paths_avoid" and not [p for p in self.protected if p]:
            raise ValueError(f"{self.key}: paths_avoid 규칙에는 protected가 필요하다")
        return self

    def hit(self, observed: int) -> bool:
        match self.op:
            case ">=":
                return observed >= self.value
            case ">":
                return observed > self.value
            case "<=":
                return observed <= self.value
            case _:
                return observed < self.value

    def paths_outside(self, paths: list[str]) -> list[str]:
        return [p for p in paths if not any(p.startswith(prefix) for prefix in self.allowed)]

    def paths_protected(self, paths: list[str]) -> list[str]:
        """보호 경로와 같거나 그 접두사로 시작하는 경로. 빈 문자열 접두사는 무시한다."""
        prefixes = [prefix for prefix in self.protected if prefix]
        return [p for p in paths if any(p.startswith(prefix) for prefix in prefixes)]


class AutoReview(_Strict):
    """사람 확인(C)으로 넘어온 건을 사람보다 먼저 판단하는 설정.

    - `auto_reject_reasons`: 이 이유로 멈춘 건은 안전 장치 규칙이 바로 거절한다(요청이 잘못된 경우)
    - `actions`: 이 액션은 `reviewer`의 `skill`(재무 AI)에게 먼저 묻는다. 승인·거절·사람에게 중 하나
    - 재무 AI가 답하지 않거나 "사람에게"라고 하면 지금처럼 사람이 결정한다
    """

    enabled: bool = False
    reviewer: str = "finance"
    skill: str = "review_hold"
    actions: list[str] = Field(default_factory=list)
    auto_reject_reasons: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=10, gt=0)


class ThresholdTable(_Strict):
    version: int = Field(ge=1)
    verify_timeout_seconds: float = Field(gt=0)
    review_timeout_seconds: float = Field(gt=0)
    rules: list[Rule] = Field(default_factory=list)
    auto_review: AutoReview = Field(default_factory=AutoReview)

    @model_validator(mode="after")
    def _unique_keys(self) -> ThresholdTable:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.key in seen:
                raise ValueError(f"기준표에 같은 규칙 키가 두 번 있다: {rule.key}")
            seen.add(rule.key)
        return self

    def rules_for(self, action: str) -> list[Rule]:
        return [rule for rule in self.rules if rule.action == action]


class Registry(_Strict):
    actions: list[ActionSpec]

    @model_validator(mode="after")
    def _unique(self) -> Registry:
        names = [spec.action for spec in self.actions]
        if len(names) != len(set(names)):
            raise ValueError("레지스트리에 같은 액션이 두 번 있다")
        return self

    def get(self, action: str) -> ActionSpec | None:
        return next((spec for spec in self.actions if spec.action == action), None)


def _read_toml(env_name: str, filename: str) -> dict[str, Any]:
    override = os.getenv(env_name)
    if override:
        with open(override, "rb") as handle:
            return tomllib.load(handle)
    text = resources.files("ax_safety").joinpath("rules", filename).read_text(encoding="utf-8")
    return tomllib.loads(text)


def load_registry() -> Registry:
    return Registry.model_validate(_read_toml("SAFETY_REGISTRY_PATH", "registry.toml"))


def load_thresholds() -> ThresholdTable:
    return ThresholdTable.model_validate(_read_toml("SAFETY_THRESHOLDS_PATH", "thresholds.toml"))


def higher(a: str, b: str) -> str:
    return a if GRADE_ORDER[a] >= GRADE_ORDER[b] else b
