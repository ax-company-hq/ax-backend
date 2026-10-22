"""판매자 사기 위험 규칙 점수 (fraud_score).

가중치는 재무(태윤) 합의 전 초안이다. 점수는 판단 재료일 뿐 승인·보류는 재무/정산이 정한다.
순수 계산이며 LLM을 쓰지 않는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Level = Literal["low", "mid", "high"]

# 규칙 코드
NEW_ACCOUNT = "NEW_ACCOUNT"
LOW_PRICE = "LOW_PRICE"
DUP_BUSINESS = "DUP_BUSINESS"
BULK_LISTING = "BULK_LISTING"
FRAUD_SIMILAR = "FRAUD_SIMILAR"

# 규칙별 최대 점수 (초안, 합계 100)
WEIGHTS: dict[str, int] = {
    NEW_ACCOUNT: 15,
    LOW_PRICE: 25,
    DUP_BUSINESS: 20,
    BULK_LISTING: 15,
    FRAUD_SIMILAR: 25,
}

NEW_ACCOUNT_DAYS = 7  # 가입 7일 미만
BULK_LISTING_COUNT = 50  # 24시간 등록 50개 이상
MID_LEVEL_MIN = 30
HIGH_LEVEL_MIN = 70


@dataclass(frozen=True)
class SellerFacts:
    joined_days: int
    low_price_ratio: float  # 0~1
    business_hash_duplicated: bool
    listings_last_24h: int
    max_fraud_card_similarity: float  # 0~1


@dataclass(frozen=True)
class FraudReason:
    rule_code: str
    value: float
    points: float


@dataclass(frozen=True)
class FraudScore:
    score: int  # 0~100
    level: Level
    reasons: list[FraudReason]


def _validate(f: SellerFacts) -> None:
    if f.joined_days < 0:
        raise ValueError("joined_days must be >= 0")
    if f.listings_last_24h < 0:
        raise ValueError("listings_last_24h must be >= 0")
    if not 0.0 <= f.low_price_ratio <= 1.0:
        raise ValueError("low_price_ratio must be in [0, 1]")
    if not 0.0 <= f.max_fraud_card_similarity <= 1.0:
        raise ValueError("max_fraud_card_similarity must be in [0, 1]")


def score_seller(facts: SellerFacts) -> FraudScore:
    """판매자 사실로 0~100 정수 점수와 위험 등급, 규칙 근거를 계산한다.

    가중치는 재무(태윤) 합의 전 초안이다. 입력이 범위 밖이면 ValueError.
    """
    _validate(facts)
    candidates: list[tuple[str, float, float]] = [
        (
            NEW_ACCOUNT,
            float(facts.joined_days),
            float(WEIGHTS[NEW_ACCOUNT]) if facts.joined_days < NEW_ACCOUNT_DAYS else 0.0,
        ),
        (LOW_PRICE, facts.low_price_ratio, WEIGHTS[LOW_PRICE] * facts.low_price_ratio),
        (
            DUP_BUSINESS,
            1.0 if facts.business_hash_duplicated else 0.0,
            float(WEIGHTS[DUP_BUSINESS]) if facts.business_hash_duplicated else 0.0,
        ),
        (
            BULK_LISTING,
            float(facts.listings_last_24h),
            float(WEIGHTS[BULK_LISTING]) if facts.listings_last_24h >= BULK_LISTING_COUNT else 0.0,
        ),
        (
            FRAUD_SIMILAR,
            facts.max_fraud_card_similarity,
            WEIGHTS[FRAUD_SIMILAR] * facts.max_fraud_card_similarity,
        ),
    ]
    reasons = [FraudReason(code, value, pts) for code, value, pts in candidates if pts > 0]
    total = sum(r.points for r in reasons)
    score = min(100, math.floor(total + 0.5))
    if score >= HIGH_LEVEL_MIN:
        level: Level = "high"
    elif score >= MID_LEVEL_MIN:
        level = "mid"
    else:
        level = "low"
    return FraudScore(score=score, level=level, reasons=reasons)
