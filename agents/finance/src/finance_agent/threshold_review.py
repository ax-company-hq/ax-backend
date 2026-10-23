"""재무 AI 기준 바꾸기 판단 (`review_threshold_change`).

safety가 안전 장치(원래 값의 0.5~2배, 마감 1시간~3일, 바꿀 수 있는 기준)를 먼저 코드로 검사하고,
그 안의 요청만 여기로 온다. 판단은 `ai_review_policy.toml`의 `[threshold_change]`와 safety가 센
`stats`로만 한다. 요청한 사람이 쓴 이유(`untrusted`)는 읽지 않는다.

- 변경 폭 |새 값 − 지금 값| / 지금 값 > `max_step_ratio` → escalate (사람이 결정)
- 낮추기(더 엄격하게) → approve
- `review_timeout_seconds` → 폭 안이면 approve
- 올리기 → 최근 24시간 그 기준에 걸린 보류가 `min_holds_to_raise`건 이상이면 approve, 아니면
  escalate("근거 부족")
"""

from __future__ import annotations

from typing import Any

from finance_agent.review import ThresholdChangePolicy
from finance_agent.schemas import ThresholdReviewInput, ThresholdReviewOutput

TIMEOUT_KEY = "review_timeout_seconds"

KEY_KO = {
    "coupon_total": "쿠폰 한 번 총액",
    "coupon_unit": "쿠폰 한 장 금액",
    "coupon_daily": "하루 쿠폰 합계",
    "compensation_per_context": "장애 한 건 보상 합계",
    "compensation_per_customer": "고객 한 명 보상 합계",
    "refund_single": "환불 한 건",
    "seller_risk": "판매자 위험 점수",
    "pr_changed_lines": "PR 바뀐 줄 수",
    "pr_changed_files": "PR 바뀐 파일 수",
    TIMEOUT_KEY: "사람 결정 마감(초)",
}


def _count(value: Any) -> int:
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return value if ok else 0


def _out(decision: str, reason: str, explanation: str) -> ThresholdReviewOutput:
    return ThresholdReviewOutput(decision=decision, reason_code=reason, explanation=explanation)  # type: ignore[arg-type]


def decide_threshold(data: ThresholdReviewInput, p: ThresholdChangePolicy) -> ThresholdReviewOutput:
    name = KEY_KO.get(data.key, data.key)
    current, new = data.current_value, data.new_value
    if not current or current <= 0 or new <= 0:
        return _out(
            "reject", "invalid_values", f"{name}의 지금 값이나 새 값이 잘못되어 거절합니다."
        )
    if new == current:
        return _out("reject", "no_change", f"{name}의 새 값이 지금 값과 같아 거절합니다.")

    step = abs(new - current) / current  # 계산은 코드로
    step_pct, limit_pct = round(step * 100), round(p.max_step_ratio * 100)
    change = f"{name} {current:,} → {new:,} ({step_pct}% {'올림' if new > current else '내림'})"
    if step > p.max_step_ratio:
        return _out(
            "escalate",
            "big_step",
            f"{change}. 한 번에 바꾸는 폭이 재무 AI 재량 한도 {limit_pct}%를 넘어 "
            "재무 담당자에게 넘깁니다.",
        )
    if data.key == TIMEOUT_KEY:
        return _out(
            "approve",
            "timeout_within_step",
            f"{change}. 재량 한도 {limit_pct}% 안의 마감 조정이라 승인합니다.",
        )
    if new < current:
        return _out(
            "approve",
            "tighten",
            f"{change}. 기준을 낮추면 사람 확인이 늘어 더 안전하므로 승인합니다.",
        )
    holds = _count(data.stats.get("holds_24h"))
    if holds >= p.min_holds_to_raise:
        return _out(
            "approve",
            "raise_with_holds",
            f"{change}. 최근 24시간 이 기준 때문에 멈춘 건이 {holds}건"
            f"(기준 {p.min_holds_to_raise}건 이상)이라 기준을 올려도 된다고 판단해 승인합니다.",
        )
    return _out(
        "escalate",
        "insufficient_evidence",
        f"{change}. 근거 부족: 최근 24시간 이 기준 때문에 멈춘 건이 {holds}건"
        f"(올리려면 {p.min_holds_to_raise}건 이상 필요)이라 재무 담당자에게 넘깁니다.",
    )
