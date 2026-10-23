"""구매자 ID 형식. shop-api의 `buyer_id`는 `buy_<4자리>`(예: `buy_0001`)이고 사용자 번호 N과
`buy_` + 0으로 채운 4자리로 오간다 (shop-api 담당 재헌과 정한 규칙).

예전 기록은 숫자 `user_id`거나 잠시 쓰던 `usr_<번호>`였다. 읽을 때 `normalize`로 지금 형식에 맞춘다.
"""

from __future__ import annotations

import re
from typing import Any

PREFIX = "buy_"
_LEGACY = re.compile(r"^usr_(\d+)$")


def from_number(number: int) -> str:
    """사용자 번호 → 구매자 ID (1 → `buy_0001`)."""
    return f"{PREFIX}{number:04d}"


def normalize(value: Any) -> str:
    """숫자와 `usr_<번호>`를 `buy_<4자리>`로 바꾸고, 그 밖의 값은 문자열로 그대로 둔다."""
    if isinstance(value, int) and not isinstance(value, bool):
        return from_number(value)
    text = str(value)
    legacy = _LEGACY.match(text)
    return from_number(int(legacy.group(1))) if legacy else text
