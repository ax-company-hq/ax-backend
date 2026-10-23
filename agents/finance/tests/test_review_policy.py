"""재무 AI 판단 기준표(`ai_review_policy.toml`, 사람만 바꿈) 읽기 테스트.

재무 재설계(2026-10-23)로 `review_hold`(C 보류 건 AI 1차 판단) skill과 그 테스트는 없앴다.
기준표 파일은 그대로라서 형식 검증과 값 읽기는 계속 확인한다.
"""

from __future__ import annotations

from finance_agent.review import load_policy


def test_policy_values() -> None:
    p = load_policy()
    assert (p.coupon.max_total, p.coupon.max_customer_total, p.refund.max_amount) == (
        450_000,
        100_000,
        300_000,
    )
    assert p.seller.reject_risk_at == 90
