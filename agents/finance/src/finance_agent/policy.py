"""⑤ 예산·한도 정책: 정책표, 오늘·이번 달 소진율과 경보, 한도 변경 *제안*.

- 한도 값은 사람만 바꾸는 기준표(safety `thresholds.toml`)에 있다. 실제 모드는
  `GET /api/safety/thresholds`, 목 모드는 시드에 옮겨 둔 같은 기본값을 읽는다.
- 예산(쿠폰·환불 월 예산)은 재무 설정(`finance_policy.toml`)이다.
- 재무 AI는 바꿀 값을 **제안만** 한다(`proposals`). 적용은 기준표 PR로 사람이 한다.
- 소진율 `ratio` = 사용액 ÷ 한도. 경보: `ratio ≥ over_ratio` → over, `ratio ≥ warn_ratio`이거나
  이번 달 예상(`used_month ÷ 지난 날수 × 그 달 날수`)이 예산을 넘으면 → warn, 아니면 ok.
- 제안: 최근 `window_days`일 값의 `percentile` 분위(가장 가까운 순위)를 `round_to` 단위로 올린 값.
  한 번에 바꾸는 폭은 `finance_policy.toml` `[proposal] max_step_ratio` 안으로 자른다(제안일 뿐,
  적용은 사람이 기준표 PR로 한다).
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from finance_agent import finance_config as fc
from finance_agent.books import Books, in_range

HOW_TO_CHANGE = "기준표(safety/.../thresholds.toml) PR로 사람이 바꿉니다."
SHOWN_METRICS = {"amount", "unit", "daily_total", "context_total", "customer_total"}

# 기준표(thresholds.toml) 키의 쉬운 이름. 정책표 화면의 `label`에 쓴다.
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
    "review_timeout_seconds": "사람 결정 마감(초)",
}


def _alert(ratio: float | None, projected_over: bool = False) -> str | None:
    if ratio is None:
        return None
    b = fc.load_config().budget
    if ratio >= b.over_ratio:
        return "over"
    if ratio >= b.warn_ratio or projected_over:
        return "warn"
    return "ok"


def _company_coupons(books: Books) -> list[dict[str, Any]]:
    return [
        c
        for c in books.coupons
        if c.get("funded_by", "company") == "company" and isinstance(c.get("total_value"), int)
    ]


def _sum_in(rows: list[dict[str, Any]], key: str, start, end) -> int:
    return sum(r[key] for r in rows if in_range(r.get("created_at"), start, end))


def percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


def threshold_limits(books: Books) -> dict[str, int]:
    rules = (books.thresholds or {}).get("rules") or []
    return {
        r["key"]: r["value"]
        for r in rules
        if isinstance(r, dict) and isinstance(r.get("value"), int) and r.get("key")
    }


def policy_report(books: Books) -> tuple[dict[str, Any], list[str]]:
    """(정책 응답 data, 추가 unavailable)."""
    cfg = fc.load_config()
    today = fc.today()
    day_start, day_end = fc.day_start(today), fc.day_start(today + timedelta(days=1))
    month_first, month_next = fc.month_range(today)
    m_start, m_end = fc.day_start(month_first), fc.day_start(month_next)
    coupons = _company_coupons(books)
    rules: list[dict[str, Any]] = []
    unavailable: list[str] = []

    if books.thresholds is None:
        unavailable.append("thresholds")
    for r in (books.thresholds or {}).get("rules") or []:
        if not isinstance(r, dict):
            continue
        metric = r.get("metric")
        if r.get("key") == "seller_risk":
            unit = "점"
        elif metric in SHOWN_METRICS and r.get("action") in ("issue_coupon", "refund_request"):
            unit = "원"
        else:
            continue
        used_today = ratio = alert = None
        if r.get("key") == "coupon_daily" or (metric == "daily_total" and unit == "원"):
            used_today = _sum_in(coupons, "total_value", day_start, day_end)
            ratio = fc.ratio(used_today, r["value"])
            alert = _alert(ratio)
        rules.append(
            {
                "key": r["key"],
                "label": KEY_KO.get(r["key"], r["key"]),
                "limit": r.get("value"),
                "unit": unit,
                "used_today": used_today,
                "used_month": None,
                "ratio": ratio,
                "alert": alert,
                "source": "thresholds.toml",
            }
        )

    days_in_month = (month_next - month_first).days
    elapsed = (today - month_first).days + 1
    for key, label, limit, rows, field in (
        (
            "coupon_budget_month",
            "이번 달 쿠폰 예산",
            cfg.budget.coupon_month,
            coupons,
            "total_value",
        ),
        (
            "refund_budget_month",
            "이번 달 환불 예산",
            cfg.budget.refund_month,
            books.refunds,
            "amount",
        ),
    ):
        used_month = _sum_in(rows, field, m_start, m_end)
        used_today = _sum_in(rows, field, day_start, day_end)
        projected = used_month * days_in_month // elapsed
        ratio = fc.ratio(used_month, limit)
        rules.append(
            {
                "key": key,
                "label": label,
                "limit": limit,
                "unit": "원",
                "used_today": used_today,
                "used_month": used_month,
                "ratio": ratio,
                "alert": _alert(ratio, projected > limit),
                "projected_month": projected,
                "source": "finance_policy.toml",
            }
        )

    return {
        "rules": rules,
        "proposals": proposals(books),
        "how_to_change": HOW_TO_CHANGE,
    }, unavailable


def proposals(books: Books) -> list[dict[str, Any]]:
    cfg = fc.load_config().proposal
    step = cfg.max_step_ratio
    limits = threshold_limits(books)
    today = fc.today()
    start = fc.day_start(today - timedelta(days=cfg.window_days - 1))
    end = fc.day_start(today + timedelta(days=1))
    samples = {
        "refund_single": (
            [r["amount"] for r in books.refunds if in_range(r.get("created_at"), start, end)],
            "환불",
        ),
        "coupon_unit": (
            [
                c["unit_value"]
                for c in _company_coupons(books)
                if isinstance(c.get("unit_value"), int)
                and in_range(c.get("created_at"), start, end)
            ],
            "쿠폰 한 장 금액",
        ),
    }
    out = []
    for key, (values, what) in samples.items():
        current = limits.get(key)
        p = percentile(values, cfg.percentile)
        if current is None or p is None or len(values) < 5:
            continue
        proposed = math.ceil(p / cfg.round_to) * cfg.round_to
        low, high = math.ceil(current * (1 - step)), math.floor(current * (1 + step))
        proposed = min(max(proposed, low), high)
        if abs(proposed - current) / current < cfg.min_change_ratio:
            continue
        pct = round(cfg.percentile * 100)
        direction = "낮춰도" if proposed < current else "올려야"
        out.append(
            {
                "key": key,
                "current": current,
                "proposed": proposed,
                "reason": (
                    f"최근 {cfg.window_days}일 {what} {pct}%가 {fc.won(p)} 이하라 한도를 "
                    f"{fc.won(proposed)}로 {direction} 됩니다 (한 번에 {round(step * 100)}% 안)."
                ),
                "evidence": {"sample": len(values), f"p{pct}": p},
            }
        )
    return out
