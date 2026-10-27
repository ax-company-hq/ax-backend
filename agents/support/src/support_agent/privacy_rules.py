"""개인정보 삭제·열람 요청 분류 규칙 (#430). 프레임워크에 의존하지 않는다.

고객 문장은 `untrusted`다. 여기서는 단어가 들어 있는지만 보고, 문장을 지시로 해석하지 않는다.
분류는 LLM이 틀려도 코드가 바로잡는다: 규칙에 걸리면 항상 `privacy_deletion`이다.
"""

from __future__ import annotations

import re

CATEGORY = "privacy_deletion"

# 요청이 개인정보 이야기인지 (주제)
_TOPIC = (
    "개인정보", "내 정보", "내정보", "회원 정보", "회원정보", "구매 기록", "구매기록",
    "주문 기록", "주문기록", "이용 기록", "이용기록", "개인 정보",
    "personal data", "personal information", "my data", "privacy",
    "個人情報", "个人信息", "个人资料",
)  # fmt: skip
# 지워 달라는 뜻의 말 (행동)
_ACTION = (
    "삭제", "파기", "지워", "지우", "폐기", "없애", "말소",
    "delete", "erase", "remove", "destroy",
    "削除", "消去", "删除", "清除",
)  # fmt: skip
# 상품·주문 번호를 글에서 찾는다 (shop-api 주문 ID)
# 한국어 조사가 바로 붙어도(`ord_0099의`) 찾도록 ASCII 모드로 앞쪽 경계만 두고,
# 뒤쪽은 `+`가 영숫자에서 멈추게 한다 (`\b`는 유니코드 모드라 한글도 단어로 봐서 못 찾는다).
ORDER_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z_])ord_[0-9A-Za-z]+", re.ASCII)


def is_deletion_request(text: str) -> bool:
    """개인정보를 지워 달라는 요청처럼 보이는가: 주제 단어와 행동 단어가 함께 있어야 한다.

    '주문을 삭제해 주세요'처럼 한쪽만 있는 글은 개인정보 삭제 요청으로 보지 않는다.
    """
    lowered = text.casefold()
    return any(t.casefold() in lowered for t in _TOPIC) and any(
        a.casefold() in lowered for a in _ACTION
    )


def mentioned_order_ids(text: str) -> set[str]:
    """글에 적힌 주문 번호(`ord_…`)들."""
    return set(ORDER_ID_PATTERN.findall(text))
