"""재무 AI 판단 기준표(`ai_review_policy.toml`, 사람만 바꿈) 읽기.

재무 재설계(2026-10-23)로 C 보류 건 AI 1차 판단(`review_hold`)은 없앴다. 기준표 파일은 사람 전용
파일이라 그대로 두고, 지금은 기준 바꾸기 판단(`threshold_review.py`)과 한도 제안의 변경 폭
(`policy.py`)이 `[threshold_change]`를 쓴다.
`[coupon]`·`[refund]`·`[seller]`는 파일 형식 검증만 한다.
"""

from __future__ import annotations

import tomllib
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CouponPolicy(_Strict):
    max_total: int = Field(ge=0)
    max_unit: int = Field(ge=0)
    max_customer_total: int = Field(ge=0)
    max_daily_total: int = Field(ge=0)
    max_compensation_to_damage: float = Field(gt=0)


class RefundPolicy(_Strict):
    max_amount: int = Field(ge=0)


class SellerPolicy(_Strict):
    reject_risk_at: int = Field(ge=0, le=100)


class ThresholdChangePolicy(_Strict):
    """기준 바꾸기 판단 (`threshold_review.py`). 사람이 정하는 값."""

    max_step_ratio: float = Field(gt=0, le=1)
    min_holds_to_raise: int = Field(ge=1)


class ReviewPolicy(_Strict):
    coupon: CouponPolicy
    refund: RefundPolicy
    seller: SellerPolicy
    threshold_change: ThresholdChangePolicy


def load_policy() -> ReviewPolicy:
    text = resources.files("finance_agent").joinpath("ai_review_policy.toml").read_text("utf-8")
    return ReviewPolicy.model_validate(tomllib.loads(text))
