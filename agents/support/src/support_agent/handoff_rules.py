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

# 사람을 찾는 말과 요청 표현. 한쪽만 있으면(예: "사람이 많아요") 요청으로 보지 않는다.
_KO = re.compile(
    r"(상담원|담당자|사람|직원|매니저|책임자).{0,8}(연결|바꿔|불러|통화|직접|만나|말하고|상담하고|보내)"
    r"|(연결|바꿔|불러|통화|직접).{0,8}(상담원|담당자|사람|직원|매니저|책임자)"
)
_EN = re.compile(
    r"(talk|speak|connect|transfer|escalate|get).{0,20}(human|real person|person|agent|"
    r"representative|operator|manager|supervisor)"
    r"|(human|real person|representative|manager|supervisor).{0,12}(please|now|instead)",
    re.IGNORECASE,
)
_JA_ZH = re.compile(
    r"(担当者|オペレーター|人間|責任者).{0,6}(話|代わ|つな)|转人工|人工客服|找真人|真人客服"
)


def is_explicit_human_request(text: str) -> bool:
    """사람과 이야기하고 싶다는 요청처럼 보이는가 (ko·en·ja·zh 단어 규칙)."""
    return bool(_KO.search(text) or _EN.search(text) or _JA_ZH.search(text))


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.index(severity)
