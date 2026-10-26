"""재무 설정(`finance_policy.toml`)과 공통 도우미(시각·금액·기간).

- 시각은 UTC. "오늘"은 UTC 날짜다(화면만 KST로 보여 준다).
- 금액은 정수 원. 비율 곱셈은 `rate_amount`로 반올림(0.5 올림)해서 정수로 만든다.
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommissionConfig(_Strict):
    default_rate: float = Field(ge=0, le=1)
    category_rates: dict[str, float] = Field(default_factory=dict)

    def rate_for(self, category: str | None) -> float:
        return self.category_rates.get(category or "", self.default_rate)


class SettlementConfig(_Strict):
    cycle: str = "weekly"
    pay_days_after_period_end: int = Field(ge=0)


class PgConfig(_Strict):
    fee_rate: float = Field(ge=0, le=1)
    payout_days: int = Field(ge=0)
    fee_tolerance: int = Field(ge=0)


class BudgetConfig(_Strict):
    coupon_month: int = Field(ge=0)
    refund_month: int = Field(ge=0)
    warn_ratio: float = Field(gt=0)
    over_ratio: float = Field(gt=0)


class ProposalConfig(_Strict):
    window_days: int = Field(ge=1)
    percentile: float = Field(gt=0, le=1)
    round_to: int = Field(ge=1)
    min_change_ratio: float = Field(ge=0)
    max_step_ratio: float = Field(gt=0, le=1)


class AuditConfig(_Strict):
    near_limit_ratio: float = Field(gt=0, le=1)
    split_window_minutes: int = Field(ge=1)
    split_min_count: int = Field(ge=2)
    refund_split_window_hours: int = Field(ge=1)
    refund_split_min_count: int = Field(ge=2)
    repeat_refund_min: int = Field(ge=2)
    refund_rate_jump_ratio: float = Field(gt=1)
    refund_rate_jump_min_diff: float = Field(ge=0)
    gate_hold_min: int = Field(ge=1)
    gate_hold_jump_ratio: float = Field(gt=1)


class FinanceConfig(_Strict):
    commission: CommissionConfig
    settlement: SettlementConfig
    pg: PgConfig
    budget: BudgetConfig
    proposal: ProposalConfig
    audit: AuditConfig


@lru_cache(maxsize=1)
def load_config() -> FinanceConfig:
    text = resources.files("finance_agent").joinpath("finance_policy.toml").read_text("utf-8")
    return FinanceConfig.model_validate(tomllib.loads(text))


# ---------------------------------------------------------------- 시각
def now() -> datetime:
    """지금(UTC). 테스트는 이 함수를 바꿔 날짜를 고정한다."""
    return datetime.now(UTC)


def today() -> date:
    return now().date()


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def parse_dt(value: object) -> datetime | None:
    """ISO8601 문자열 → UTC datetime. 시간대가 없으면 UTC로 본다. 못 읽으면 None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_date(value: str | None) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"날짜 형식이 아니다(YYYY-MM-DD): {value}") from exc


WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def week_range(period: str | None, ref: date) -> tuple[str, date, date]:
    """`YYYY-Www` → (이름, 월요일, 일요일). 없으면 `ref`가 속한 주의 지난주."""
    if period:
        match = WEEK_RE.match(period)
        if not match:
            raise ValueError(f"정산 기간 형식이 아니다(YYYY-Www): {period}")
        try:
            monday = date.fromisocalendar(int(match[1]), int(match[2]), 1)
        except ValueError as exc:
            raise ValueError(f"없는 주: {period}") from exc
    else:
        monday = ref - timedelta(days=ref.weekday() + 7)
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}", monday, monday + timedelta(days=6)


def month_range(d: date) -> tuple[date, date]:
    """그 달 1일과 다음 달 1일."""
    first = d.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt


# ---------------------------------------------------------------- 금액
def rate_amount(amount: int, rate: float) -> int:
    """금액 × 비율을 원 단위로 반올림(0.5 올림)."""
    value = Decimal(amount) * Decimal(str(rate))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def won(n: int | None) -> str:
    return "확인할 수 없음" if n is None else f"{n:,}원"


def ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None
