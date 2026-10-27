"""고객 문의 접수 중계(B안, #385): 입력 검증과 속도 제한. 고객지원 호출은 `support_client`가 한다.

- 문의에는 주문이 필수다. 주문이 없거나 다른 구매자의 것이면 똑같이 `ORDER_NOT_FOUND`(404)로
  답해 남의 주문이 있는지 알려 주지 않는다.
- `buy_0007` → 고객지원에 보낼 정수 `7`. 형식이 다르면 보내지 않는다.
- 문의 원문(제목·본문)은 로그·오류 메시지에 싣지 않는다. 가공 없이 `untrusted`로만 넘긴다.
- 속도 제한은 프로세스 메모리의 1분 창이다(재시작하면 비워진다). 구매자별과 전체 두 가지:
  `SHOP_INQUIRY_RATE_PER_BUYER`(기본 5/분), `SHOP_INQUIRY_RATE_GLOBAL`(기본 60/분).
  중복 접수 창은 `SHOP_INQUIRY_DEDUP_WINDOW_SEC`(기본 600초).
  본문 상한은 `SHOP_INQUIRY_MAX_BODY`(기본 2000자), 제목은 `SHOP_INQUIRY_MAX_SUBJECT`(기본 100자).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable

from ax_shop_api.service import ShopError

BUYER_ID = re.compile(r"^buy_([0-9]{4,9})$")  # ASCII 숫자만(\d는 유니코드 숫자도 받는다)
ORDER_ID = re.compile(r"^ord_[0-9]+$")
WINDOW_SECONDS = 60.0


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def buyer_number(buyer_id: str) -> int:
    """`buy_0007` → 7. 형식이 다르면 400."""
    found = BUYER_ID.match(buyer_id)
    if found is None:
        raise ShopError("INVALID_BUYER_ID", "구매자 ID 형식이 올바르지 않다", 400)
    return int(found.group(1))


def dedup_window() -> int:
    """연타·재시도를 같은 접수로 묶는 창(초). 0 이하·잘못된 값은 기본 600초."""
    return _int_env("SHOP_INQUIRY_DEDUP_WINDOW_SEC", 600)


def digest(
    buyer_id: str,
    order_id: str,
    subject: str,
    message: str,
    now: Callable[[], float] = time.time,
) -> str:
    """같은 구간의 같은 접수(구매자·주문·제목·본문)는 같은 값이다.

    멱등 키에 원문 대신 이 해시를 쓴다. 구간 번호(`floor(epoch / 창)`)가 들어 있어 며칠 뒤 같은
    문장은 새 문의가 된다. 한계: 구간 경계에서 연타하면 키가 둘로 갈릴 수 있다(수용).
    """
    bucket = int(now() // dedup_window())
    joined = "".join((buyer_id, order_id, subject, message, str(bucket)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def check_order_id(order_id: str) -> None:
    if not ORDER_ID.match(order_id):
        raise ShopError("INVALID_ORDER_ID", "주문 번호 형식이 올바르지 않다", 400)


def clean_text(subject: str | None, body: str, order_id: str) -> tuple[str, str]:
    """제목·본문을 다듬고 길이를 확인한다. 오류 메시지에 원문을 싣지 않는다."""
    text = body.strip()
    if not text:
        raise ShopError("EMPTY_BODY", "문의 내용을 입력해야 한다", 400)
    max_body = _int_env("SHOP_INQUIRY_MAX_BODY", 2000)
    if len(text) > max_body:
        raise ShopError("BODY_TOO_LONG", f"문의 내용은 {max_body}자 이하여야 한다", 400)
    title = (subject or "").strip() or f"주문 {order_id} 문의"
    max_subject = _int_env("SHOP_INQUIRY_MAX_SUBJECT", 100)
    if len(title) > max_subject:
        raise ShopError("SUBJECT_TOO_LONG", f"제목은 {max_subject}자 이하여야 한다", 400)
    return title, text


class InquiryRateLimiter:
    """구매자별·전체 1분 창 제한. 통과한 시도만 센다."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._per_buyer: dict[str, deque[float]] = defaultdict(deque)
        self._all: deque[float] = deque()

    @staticmethod
    def _trim(stamps: deque[float], now: float) -> None:
        while stamps and now - stamps[0] >= WINDOW_SECONDS:
            stamps.popleft()

    def hit(self, buyer_id: str) -> None:
        now = self._clock()
        mine = self._per_buyer[buyer_id]
        self._trim(mine, now)
        self._trim(self._all, now)
        if len(mine) >= _int_env("SHOP_INQUIRY_RATE_PER_BUYER", 5):
            raise ShopError(
                "RATE_LIMITED", "문의를 너무 자주 보냈다. 잠시 후 다시 시도해 달라", 429
            )
        if len(self._all) >= _int_env("SHOP_INQUIRY_RATE_GLOBAL", 60):
            raise ShopError("RATE_LIMITED", "문의가 몰려 있다. 잠시 후 다시 시도해 달라", 429)
        mine.append(now)
        self._all.append(now)
