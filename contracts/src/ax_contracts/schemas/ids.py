"""ID 접두사 규칙 (CLAUDE.md "표기·형식").

ID는 `접두사 + 문자열`이다. 이 모듈은 접두사 이름만 모아 두며 검증 로직은 넣지 않는다.
테스트나 로컬 실행에서는 접두사를 지키지 않는 값도 들어올 수 있어서 모델이 강제하지 않는다.
"""

from __future__ import annotations

from enum import StrEnum


class IdPrefix(StrEnum):
    """일반 ID 접두사."""

    CONTEXT = "ctx_"
    EVENT = "evt_"
    ORDER = "ord_"
    PAYMENT = "pay_"
    PRODUCT = "prd_"
    SELLER = "sel_"
    BUYER = "buy_"  # 구매자(가명 ID, `buy_` + 4자리). 개인정보를 담지 않는다
    RETURN = "ret_"
    REFUND = "rfd_"
    CLAIM = "clm_"  # 고객지원 클레임. 이벤트 data에는 넣지 않는다
    COUPON = "cpn_"
    CAMPAIGN = "cmp_"
    NOTICE = "ntc_"
    INQUIRY = "inq_"
    EXECUTION = "exe_"
    REVIEW = "rev_"
    CARD = "card_"


class ContextPrefix(StrEnum):
    """`context_id` 접두사. 사건 하나 = `context_id` 하나다."""

    INCIDENT = "ctx_inc_"  # 사건. 통합 에이전트만 발급한다
    REQUEST = "ctx_req_"  # 변경 요청. planning이 발급한다
    NOTICE = "ctx_ntc_"  # 공지·패턴
    CAMPAIGN = "ctx_cmp_"  # 캠페인. marketing이 발급한다
