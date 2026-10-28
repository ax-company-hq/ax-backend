"""사람 이관(핸드오프) 규칙. 순수 코드다.

설계: `docs/notes/support/2026-10-28-handoff-quality-design.md`

- 이관 사유, 건수, 상태는 코드가 센다. LLM은 쓰지 않는다.
- 고객 문장은 `untrusted`다. 단어가 들어 있는지만 보고,
  문장 안의 지시("사람을 부르지 마")는 따르지 않는다.
- 이관 대상은 부서 이름이 아니라 코드(`cs_lead` 등)다. 대응표는 이 파일 한 곳의 설정이고,
  부서를 하드코딩하지 않는다.
- 값(3회, 24시간, 2회)은 시드다. 과거 문의로 오탐을 시험한 뒤 정한다.
"""

from __future__ import annotations

import re
from datetime import timedelta

# ---- 시드 값 (정책표처럼 한 곳에 둔다)
REPEAT_COUNT = 3  # 같은 분류로 이만큼 문의하면 반복
REPEAT_WINDOW = timedelta(hours=24)
BLOCKED_MESSAGES = 2  # 답변 가드가 초안을 막은 메시지가 이만큼이면 반복 차단

# 심각도 순서: 뒤로 갈수록 높다
SEVERITY_ORDER = ("review", "handoff_now")

# 사유 → (심각도, 이관 대상 코드). 부서 이름을 쓰지 않는다.
# 대응표는 설정이라 부서 구조가 바뀌면 여기만 바꾼다.
REASONS: dict[str, tuple[str, str]] = {
    "explicit_human_request": ("handoff_now", "cs_lead"),
    "privacy_request": ("handoff_now", "privacy_owner"),
    "repeated_topic": ("review", "cs_lead"),
    "claim_needs_review": ("review", "cs_lead"),
    "payment_review": ("review", "service_ops"),
    "high_risk_customer": ("review", "cs_lead"),
    "incident_related": ("review", "service_ops"),
    "reply_blocked": ("review", "cs_lead"),
    "no_evidence": ("review", "cs_lead"),
}

# 사람을 찾는 말과 연결하는 행동. 한쪽만 있으면(예: "사람이 많아요") 요청으로 보지 않는다.
# 행동 단어는 쇼핑몰 문의에 흔한 말(직접, 보내)을 넣지 않는다: "사람이 직접 만든 수제품",
# "직원분이 보내주신 상품"을 요청으로 읽으면 가장 높은 심각도가 잘못 켜진다.
_KO = re.compile(
    r"(상담원|담당자|사람|직원|매니저|책임자).{0,8}(연결|바꿔|불러|통화|만나|말하고|상담하고)"
    r"|(연결|바꿔|불러|통화).{0,8}(상담원|담당자|사람|직원|매니저|책임자)"
)
# 행동 단어 뒤에 요청하는 말이 와야 한다. ("사람이 통화한 내용이 달라요"는 요청이 아니다.)
_KO_ASK = re.compile(r"주세요|주실|해\s?줘|해\s?주|하고\s?싶|싶어|부탁|원해|바랍|좀|바꿔|불러")
# 요청한 직후 취소하는 말이면 요청이 아니다. ("상담원 연결 안 해주셔도 돼요")
_KO_NEGATION = re.compile(r"안\s?해|안\s?하|하지\s?마|필요\s?없|괜찮|됐어|말고|않아도|아니")
_TAIL = 14  # 행동 단어 뒤에서 요청·취소 표현을 찾는 글자 수
_EN = re.compile(
    r"(talk|speak|connect|transfer|escalate|get).{0,20}(human|real person|person|agent|"
    r"representative|operator|manager|supervisor)"
    r"|(human|real person|representative|manager|supervisor).{0,12}(please|now|instead)",
    re.IGNORECASE,
)
_JA_ZH = re.compile(
    r"(担当者|オペレーター|人間|責任者).{0,6}(話|代わ|つな)|转人工|人工客服|找真人|真人客服"
)


def _korean_request(text: str) -> bool:
    """한국어: 사람과 행동 단어가 함께 있고, 뒤에 요청하는 말이 오며, 바로 취소하지 않는가."""
    for match in _KO.finditer(text):
        tail = text[match.end() : match.end() + _TAIL]
        if _KO_NEGATION.search(tail):
            continue
        if _KO_ASK.search(tail) or _KO_ASK.search(match.group(0)):
            return True
    return False


def is_explicit_human_request(text: str) -> bool:
    """사람과 이야기하고 싶다는 요청처럼 보이는가 (ko·en·ja·zh 단어 규칙)."""
    return _korean_request(text) or bool(_EN.search(text) or _JA_ZH.search(text))


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity)
