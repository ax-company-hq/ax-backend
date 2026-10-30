"""① 결산·리포트: 일·월 손익 요약, 자동 분개 미리보기, 쉬운 말 설명.

숫자는 모두 코드가 계산한다 (공식은 아래). 설명 문장만 LLM이 쓸 수 있고, 목 모드·LLM 미설정·
실패 때는 코드 템플릿 문장(`commentary_source="template"`)을 쓴다. LLM에는 계산된 숫자만 준다.

- 총거래액 `gmv` = 기간 안에 결제된 주문의 품목 판매가 합계 (수량 × 단가, 적립금 차감 전)
- 판매 수수료 매출 `commission_revenue` = 품목별 판매가 × 카테고리 수수료율(반올림) 합계
- 쿠폰 비용 `coupon_cost` = 기간 안에 발급한 회사 부담 쿠폰 총액 (판매자 부담 쿠폰 제외)
- 환불 `refund_total` = 기간 안 환불 합계 (판매자 정산에서 차감되므로 영업 결과에는 넣지 않는다)
- PG 수수료 `pg_fee` = 기간 안 결제의 PG 수수료 (PG 정산 기록이 있으면 그 값, 없으면 기본 수수료율)
- 영업 결과 `net_result` = 수수료 매출 − 쿠폰 비용 − PG 수수료

받지 못한 자료로 계산하는 숫자는 0원이 아니라 `None`(확인할 수 없음)으로 둔다.
주문 자료가 없으면 총거래액·수수료 매출·환불·PG 수수료·영업 결과가,
쿠폰 자료가 없으면 쿠폰 비용·영업 결과가 `None`이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from ax_agent_template.llm import LLMClient
from finance_agent import finance_config as fc
from finance_agent.books import Books, in_range, item_gross, order_sold, pg_settlements

logger = logging.getLogger("ax.finance.closing")

Period = Literal["day", "week", "month"]

LINE_LABELS = {
    "gmv": "총거래액",
    "commission_revenue": "판매 수수료 매출",
    "coupon_cost": "쿠폰 비용(회사 부담)",
    "refund_total": "환불",
    "pg_fee": "PG 수수료",
    "net_result": "영업 결과",
}

# 자료 이름(`Books.unavailable`) → 그 자료가 없으면 확인할 수 없는 줄
NEEDS = {
    "orders": ("gmv", "commission_revenue", "refund_total", "pg_fee", "net_result"),
    "coupons": ("coupon_cost", "net_result"),
}


def unknown_codes(books: Books) -> frozenset[str]:
    """받지 못한 자료 때문에 숫자를 낼 수 없는 줄. 일부만 빠진 `orders_partial`은 넣지 않는다."""
    missing = [codes for name, codes in NEEDS.items() if name in books.unavailable]
    return frozenset(code for codes in missing for code in codes)


@dataclass(frozen=True)
class Totals:
    gmv: int
    commission_revenue: int
    coupon_cost: int
    refund_total: int
    pg_fee: int
    order_count: int

    @property
    def net_result(self) -> int:
        return self.commission_revenue - self.coupon_cost - self.pg_fee

    def as_kpis(self, unknown: frozenset[str] = frozenset()) -> dict[str, int | None]:
        values = {
            "gmv": self.gmv,
            "commission_revenue": self.commission_revenue,
            "refund_total": self.refund_total,
            "coupon_cost": self.coupon_cost,
            "pg_fee": self.pg_fee,
            "net_result": self.net_result,
        }
        return {k: None if k in unknown else v for k, v in values.items()}


def period_range(period: Period, d: date) -> tuple[date, date]:
    """(시작일, 끝 다음 날). 주면 그 주 월요일 ~ 다음 주 월요일, 월이면 그 달 1일 ~ 다음 달 1일."""
    if period == "month":
        return fc.month_range(d)
    if period == "week":
        monday = d - timedelta(days=d.weekday())
        return monday, monday + timedelta(days=7)
    return d, d + timedelta(days=1)


def compute_totals(books: Books, start: datetime, end: datetime) -> Totals:
    cfg = fc.load_config()
    pg = pg_settlements(books)
    gmv = commission = pg_fee = orders = 0
    for o in books.orders:
        if not order_sold(o):
            continue
        paid_at = min(p["created_at"] for p in o["payments"] if p.get("status") == "succeeded")
        if not in_range(paid_at, start, end):
            continue
        orders += 1
        for item in o["items"]:
            gross = item_gross(item)
            gmv += gross
            commission += fc.rate_amount(gross, cfg.commission.rate_for(item.get("category_code")))
        for p in o["payments"]:
            if p.get("status") != "succeeded":
                continue
            record = pg.get(p["payment_id"])
            pg_fee += record["fee"] if record else fc.rate_amount(p["amount"], cfg.pg.fee_rate)
    coupon_cost = sum(
        c["total_value"]
        for c in books.coupons
        if c.get("funded_by", "company") == "company"
        and isinstance(c.get("total_value"), int)
        and in_range(c.get("created_at"), start, end)
    )
    refund_total = sum(
        r["amount"] for r in books.refunds if in_range(r.get("created_at"), start, end)
    )
    return Totals(gmv, commission, coupon_cost, refund_total, pg_fee, orders)


def lines(t: Totals, unknown: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    values = {
        "gmv": t.gmv,
        "commission_revenue": t.commission_revenue,
        "coupon_cost": -t.coupon_cost,
        "refund_total": -t.refund_total,
        "pg_fee": -t.pg_fee,
        "net_result": t.net_result,
    }
    return [
        {"code": k, "label": LINE_LABELS[k], "amount": None if k in unknown else v}
        for k, v in values.items()
    ]


def journal(t: Totals, unknown: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """자동 분개 미리보기. 각 묶음은 차변 합계 = 대변 합계. 확인할 수 없는 묶음은 뺀다."""
    groups = [
        (
            "gmv",
            [
                ("외상매출금(PG)", t.gmv, 0, "카드 결제 매출"),
                ("수수료 매출", 0, t.commission_revenue, "판매 수수료"),
                ("판매자 예수금", 0, t.gmv - t.commission_revenue, "판매자에게 줄 판매 대금"),
            ],
        ),
        (
            "pg_fee",
            [
                ("지급수수료(PG)", t.pg_fee, 0, "PG 결제 수수료"),
                ("외상매출금(PG)", 0, t.pg_fee, "PG 수수료 차감"),
            ],
        ),
        (
            "coupon_cost",
            [
                ("판매촉진비(쿠폰)", t.coupon_cost, 0, "회사 부담 쿠폰"),
                ("미지급금(쿠폰)", 0, t.coupon_cost, "쿠폰 사용분 정산 예정"),
            ],
        ),
        (
            "refund_total",
            [
                ("판매자 예수금", t.refund_total, 0, "환불(판매자 정산에서 차감)"),
                ("외상매출금(PG)", 0, t.refund_total, "카드 결제 취소"),
            ],
        ),
    ]
    entries = [e for code, rows in groups if code not in unknown for e in rows]
    return [
        {"account": a, "debit": dr, "credit": cr, "memo": m} for a, dr, cr, m in entries if dr or cr
    ]


def period_label(period: Period, d: date, today: date) -> str:
    if period == "month":
        return f"{d.year}년 {d.month}월"
    if period == "week":
        monday, _ = period_range("week", d)
        return f"{monday.month}월 {monday.day}일부터 한 주"
    if d == today:
        return "오늘"
    if d == today - timedelta(days=1):
        return "어제"
    return f"{d.month}월 {d.day}일"


def template_commentary(label: str, t: Totals, unknown: frozenset[str] = frozenset()) -> str:
    """LLM 없이 쓰는 설명 문장. 숫자는 모두 `t`(코드 계산값)에서 온다."""
    if "gmv" in unknown:
        coupon = (
            "" if "coupon_cost" in unknown else f" 회사 부담 쿠폰은 {fc.won(t.coupon_cost)}입니다."
        )
        return (
            f"주문 자료를 받지 못해 {label} 총거래액·수수료 매출·영업 결과는 확인할 수 없습니다."
            + coupon
        )
    if "coupon_cost" in unknown:
        return (
            f"{label} 총거래액은 {fc.won(t.gmv)}(주문 {t.order_count}건)이고, 판매 수수료 매출은 "
            f"{fc.won(t.commission_revenue)}입니다. 쿠폰 자료를 받지 못해 영업 결과는 확인할 수 "
            "없습니다."
        )
    if t.order_count == 0 and not t.coupon_cost and not t.refund_total:
        return f"{label} 기준으로 아직 결제된 주문이 없습니다."
    result = "흑자" if t.net_result >= 0 else "적자"
    return (
        f"{label} 총거래액은 {fc.won(t.gmv)}(주문 {t.order_count}건)이고, 판매 수수료 매출은 "
        f"{fc.won(t.commission_revenue)}입니다. 회사 부담 쿠폰 {fc.won(t.coupon_cost)}과 "
        f"PG 수수료 {fc.won(t.pg_fee)}을 빼면 영업 결과는 {fc.won(t.net_result)}({result})입니다. "
        f"환불 {fc.won(t.refund_total)}은 판매자 정산에서 차감됩니다."
    )


class Commentary(BaseModel):
    """LLM 응답 형식. 문장만 받는다."""

    text: str = Field(min_length=1, max_length=600)


async def write_commentary(
    label: str, t: Totals, mock: bool, unknown: frozenset[str] = frozenset()
) -> tuple[str, str]:
    """(설명, 출처). 목 모드이거나 LLM 설정이 없거나 실패하면 템플릿 문장.

    확인할 수 없는 숫자가 있으면 LLM에 넘기지 않는다 (0원을 숫자로 읽고 설명할 수 있어서).
    """
    fallback = template_commentary(label, t, unknown)
    if mock or unknown:
        return fallback, "template"
    client = LLMClient("finance")
    if not client.settings.model:
        return fallback, "template"
    numbers = {"기간": label, "주문 수": t.order_count} | {
        LINE_LABELS[k]: v for k, v in t.as_kpis().items()
    }
    prompt = (
        "너는 캣북스 재무팀이다. 아래 손익 숫자(정수 원, 코드가 계산함)를 대표가 읽을 쉬운 한국어 "
        "두세 문장으로 설명하라. 숫자를 새로 계산하거나 바꾸지 말고 주어진 값만 쓴다.\n"
        f"{numbers}"
    )
    try:
        out = await client.structured(prompt, Commentary)
    except Exception:  # 미구현·네트워크·스키마 오류 모두 템플릿으로
        logger.info("결산 설명 LLM 실패, 템플릿 문장을 쓴다")
        return fallback, "template"
    return out.text, "llm"


async def closing_report(
    books: Books,
    period: Period,
    d: date,
    confirmation: dict[str, Any] | None,
) -> dict[str, Any]:
    start_d, end_d = period_range(period, d)
    start, end = fc.day_start(start_d), fc.day_start(end_d)
    t = compute_totals(books, start, end)
    unknown = unknown_codes(books)
    label = period_label(period, d, fc.today())
    commentary, source = await write_commentary(label, t, books.mock, unknown)
    shown_date = d if period == "day" else start_d
    return {
        "period": period,
        "date": shown_date.isoformat(),
        "from": fc.iso(start),
        "to": fc.iso(end),
        "lines": lines(t, unknown),
        "journal": journal(t, unknown),
        "commentary": commentary,
        "commentary_source": source,
        "status": "confirmed" if confirmation else "draft",
        "confirmed_by": confirmation["by"] if confirmation else None,
        "confirmed_at": confirmation["at"] if confirmation else None,
    }
