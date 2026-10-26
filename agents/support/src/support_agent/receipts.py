"""영수증 분석: 글(OCR·붙여넣은 글)에서 사실을 뽑고 주문과 대조한다. 프레임워크 무관 코드다.

- 금액·날짜·주문번호·품목은 **코드(정규식)가 뽑는다**. 이미지는 `VisionExtractor`를 꽂았을
  때만 읽고, 꽂지 않았으면 읽지 못했다고 알린다 (지어내지 않는다). LLM이 판정하지 않는다.
- 글은 개인정보(카드번호·전화번호·계좌 등)를 가린 뒤에 읽는다. 결과에는 뽑은 사실만 담고 원문은
  담지 않으며, 어디에도 저장하지 않는다.
- 주문 대조(`compare`)는 같음·다름·확인할 수 없음만 코드로 말한다. 환불 판정은 `claim_rules`가 한다.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone
from difflib import SequenceMatcher

from support_agent.orders import Order

KST = timezone(timedelta(hours=9))
ITEM_MATCH_RATIO = 0.75  # 영수증 품목 이름이 주문 품목과 같다고 보는 유사도
DATE_TOLERANCE_DAYS = 1  # 결제 시각의 날짜 표기가 하루쯤 어긋나는 것(UTC·KST)은 허용한다

# 총 결제 금액을 가리키는 말. 위쪽 묶음이 있으면 그것만 본다 (상품 합계, 배송비 줄과 섞이지 않게)
_TOTAL_STRONG = re.compile(
    r"총\s?결제|결제\s?금액|최종\s?결제|받을\s?금액|grand\s?total|amount\s?paid|total\s?paid",
    re.IGNORECASE,
)
_TOTAL_WEAK = re.compile(r"총\s?금액|총\s?액|합\s?계|\btotal\b", re.IGNORECASE)
_NOT_ITEM = re.compile(
    r"합\s?계|총\s?액|총\s?금액|결제|배송비|포인트|적립|할인|쿠폰|부가세|total|subtotal|shipping|tax",
    re.IGNORECASE,
)
_AMOUNT = re.compile(r"(?<![\d,])(\d{1,3}(?:,\d{3})+|\d{3,9})(?:\s?원)?(?![\d,])")
_DATE_NUMERIC = re.compile(r"(?<!\d)(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)")
_DATE_KOREAN = re.compile(r"(20\d{2})\s?년\s?(\d{1,2})\s?월\s?(\d{1,2})\s?일")
_ORDER_NO = re.compile(
    r"\b(ord_\d+|ORD-\d{4}-\d{3,6})\b|주문\s?번호\s?[:：]?\s?([A-Za-z0-9][A-Za-z0-9_-]{3,29})",
    re.IGNORECASE,
)
_SHIPPING = re.compile(
    r"(?:배송비|shipping)\s?[:：]?\s?(\d{1,3}(?:,\d{3})+|\d{1,7})", re.IGNORECASE
)
_ITEM_LINE = re.compile(
    r"^\s*(?P<title>[^\d\s][^\n]*?)\s+(?P<qty>\d{1,2})\s*(?:개|ea|EA|x|X)?\s+"
    r"(?P<amount>\d{1,3}(?:,\d{3})+|\d{3,7})\s*원?\s*$"
)


@dataclass
class ReceiptItem:
    title: str
    qty: int
    amount: int


@dataclass
class ReceiptFacts:
    """영수증에서 뽑은 사실. 못 찾은 것은 None이다 (0이나 추측으로 채우지 않는다)."""

    total: int | None = None
    paid_on: date | None = None
    order_no: str | None = None
    shipping_fee: int | None = None
    items: list[ReceiptItem] = field(default_factory=list)
    ambiguous_total: bool = False  # 총 결제 금액 후보가 서로 다르면 고르지 않는다

    def found_anything(self) -> bool:
        return any(
            (self.total is not None, self.paid_on, self.order_no, self.shipping_fee, self.items)
        )


def to_amount(text: str) -> int:
    return int(text.replace(",", ""))


def _amounts(line: str) -> list[int]:
    return [to_amount(m.group(1)) for m in _AMOUNT.finditer(line)]


def parse_receipt_text(text: str) -> ReceiptFacts:
    """(개인정보를 가린) 영수증 글에서 사실을 뽑는다."""
    facts = ReceiptFacts()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    strong: list[int] = []
    weak: list[int] = []
    for line in lines:
        found = _amounts(line)
        if not found:
            continue
        if _TOTAL_STRONG.search(line):
            strong.append(found[-1])
        elif _TOTAL_WEAK.search(line) and not re.search(
            r"소계|sub\s?total|상품", line, re.IGNORECASE
        ):
            weak.append(found[-1])
    chosen = strong or weak
    if chosen:
        distinct = set(chosen)
        if len(distinct) == 1:
            facts.total = distinct.pop()
        else:  # 서로 다른 총액 후보: 어느 것이 맞는지 모르니 고르지 않는다
            facts.ambiguous_total = True

    for pattern in (_DATE_NUMERIC, _DATE_KOREAN):
        for match in pattern.finditer(text):
            try:
                facts.paid_on = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
            break
        if facts.paid_on:
            break

    order_match = _ORDER_NO.search(text)
    if order_match:
        facts.order_no = (order_match.group(1) or order_match.group(2)).upper()

    shipping = _SHIPPING.search(text)
    if shipping:
        facts.shipping_fee = to_amount(shipping.group(1))

    for line in lines:
        if _NOT_ITEM.search(line):
            continue
        match = _ITEM_LINE.match(line)
        if match:
            facts.items.append(
                ReceiptItem(match["title"].strip(), int(match["qty"]), to_amount(match["amount"]))
            )
    return facts


# ---------------------------------------------------------------- 이미지 (나중에 꽂는다)
@dataclass
class EvidenceImage:
    ref: str  # 사진이 저장된 곳의 참조 (예: 저장소 키)
    media_type: str = "image/jpeg"
    data_base64: str | None = None  # 사진 자체. 크기를 제한하고, 어디에도 저장하지 않는다


VisionExtractor = Callable[[list[EvidenceImage]], Awaitable[ReceiptFacts]]
_vision: VisionExtractor | None = None


def set_vision_extractor(extractor: VisionExtractor | None) -> None:
    """이미지에서 영수증 사실을 읽는 함수를 꽂는다 (LLM 이미지 입력이 연결되면). None이면 해제다."""
    global _vision
    _vision = extractor


def get_vision_extractor() -> VisionExtractor | None:
    return _vision


# ---------------------------------------------------------------- 주문 대조
@dataclass
class ReceiptCheck:
    verdict: str  # consistent | mismatch | insufficient
    checked: list[str] = field(default_factory=list)  # 대조한 항목
    mismatches: list[str] = field(default_factory=list)  # RECEIPT_… 코드
    matched_basis: str | None = None  # 총액이 맞은 기준: cash(실결제) | gross(포인트 전)
    unmatched_receipt_items: list[str] = field(default_factory=list)
    missing_from_receipt: list[str] = field(
        default_factory=list
    )  # 주문에는 있는데 영수증에 없는 품목


def order_totals(order: Order) -> tuple[int, int]:
    """`(실결제 현금, 포인트 쓰기 전 총액)`. 배송비를 포함한다."""
    gross = sum(i.paid_amount + i.points_share for i in order.items) + order.shipping_fee
    return gross - order.points_used, gross


def _similar(a: str, b: str) -> float:
    a, b = a.casefold().strip(), b.casefold().strip()
    if a and (a in b or b in a):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def compare(facts: ReceiptFacts, order: Order) -> ReceiptCheck:
    """영수증 사실을 주문과 견준다. 같음·다름만 말하고 환불 판정은 하지 않는다."""
    check = ReceiptCheck(verdict="insufficient")
    cash, gross = order_totals(order)

    if facts.total is not None:
        check.checked.append("total")
        if facts.total == cash:
            check.matched_basis = "cash"
        elif facts.total == gross:
            check.matched_basis = "gross"
        else:
            check.mismatches.append("RECEIPT_AMOUNT_MISMATCH")

    if facts.paid_on is not None:
        check.checked.append("date")
        paid = order.paid_at.astimezone(KST).date()
        if abs((facts.paid_on - paid).days) > DATE_TOLERANCE_DAYS:
            check.mismatches.append("RECEIPT_DATE_MISMATCH")

    if facts.order_no is not None:
        check.checked.append("order_no")
        known = {order.id.upper(), order.order_no.upper()}
        if facts.order_no not in known:
            check.mismatches.append("RECEIPT_ORDER_NO_MISMATCH")

    if facts.shipping_fee is not None:
        check.checked.append("shipping_fee")
        if facts.shipping_fee != order.shipping_fee:
            check.mismatches.append("RECEIPT_SHIPPING_MISMATCH")

    if facts.items:
        check.checked.append("items")
        remaining = list(order.items)
        for line in facts.items:
            best = max(remaining, key=lambda i: _similar(line.title, i.title), default=None)
            if best is None or _similar(line.title, best.title) < ITEM_MATCH_RATIO:
                check.unmatched_receipt_items.append(line.title)
                continue
            remaining.remove(best)
            if line.amount != best.paid_amount + best.points_share:
                if "RECEIPT_ITEM_AMOUNT_MISMATCH" not in check.mismatches:
                    check.mismatches.append("RECEIPT_ITEM_AMOUNT_MISMATCH")
        check.missing_from_receipt = [i.title for i in remaining]
        if check.unmatched_receipt_items:
            check.mismatches.append("RECEIPT_ITEM_UNKNOWN")

    if check.mismatches:
        check.verdict = "mismatch"
    elif check.checked:
        check.verdict = "consistent"
    return check
