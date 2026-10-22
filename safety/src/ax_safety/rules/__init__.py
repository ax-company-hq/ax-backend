"""액션 등급 레지스트리와 기준 금액 등급표 (SAF-02).

설정 파일(`registry.toml`, `thresholds.toml`)은 사람만 바꾼다. 여기는 읽고 검증만 한다.
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
    action: str
    owner: str
    base_grade: Grade
    verifier: str | None = None
    verify_skill: str | None = None
    amount: AmountSpec | None = None
    customer_field: str | None = None
    purpose_field: str | None = None

    @model_validator(mode="after")
    def _verifier_rules(self) -> ActionSpec:
        if self.base_grade == "B" and not (self.verifier and self.verify_skill):
            raise ValueError(f"{self.action}: B 등급에는 verifier와 verify_skill이 필요하다")
        if self.verifier is not None and self.verifier == self.owner:
            raise ValueError(f"{self.action}: 자기 검증 금지 (verifier == owner)")
        return self


class Rule(_Strict):
    key: str
    action: str
    metric: Literal["amount", "unit", "daily_total", "context_total", "customer_total"]
    op: Literal[">=", ">"]
    value: int = Field(ge=0)
    when: dict[str, Any] = Field(default_factory=dict)

    def hit(self, observed: int) -> bool:
        return observed >= self.value if self.op == ">=" else observed > self.value


class ThresholdTable(_Strict):
    version: int = Field(ge=1)
    verify_timeout_seconds: float = Field(gt=0)
    review_timeout_seconds: float = Field(gt=0)
    rules: list[Rule] = Field(default_factory=list)

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
