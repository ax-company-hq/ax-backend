"""답변 가드: 고객에게 나가는 답변을 코드 규칙으로 검사한다. LLM을 쓰지 않는다.

`error`가 하나라도 있으면 통과하지 못한다(`passed=False`). 경고(`warning`)는 통과를 막지 않는다.
"""

from __future__ import annotations

import re

from support_agent.schemas import Finding, Validation

PHONE = re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 환불·보상을 단정하거나 약속하는 표현
PROMISE = re.compile(
    r"(환불|보상|배상).{0,12}(약속|보장|확정|무조건|반드시)|(무조건|반드시).{0,8}(환불|보상|배상)"
)
MAX_LENGTH = 1200


def mask_pii(text: str) -> tuple[str, list[str]]:
    """전화번호·이메일을 가리고, 가린 종류를 함께 돌려준다 (LLM에 넘기기 전에 쓴다)."""
    kinds: list[str] = []
    if PHONE.search(text):
        kinds.append("phone")
        text = PHONE.sub("[전화번호]", text)
    if EMAIL.search(text):
        kinds.append("email")
        text = EMAIL.sub("[이메일]", text)
    return text, kinds


def check_reply(text: str) -> Validation:
    findings: list[Finding] = []
    if not text.strip():
        findings.append(Finding(code="empty_reply", level="error", message="답변이 비어 있다"))
    if PROMISE.search(text):
        findings.append(
            Finding(code="promise_refund", level="error", message="환불·보상을 단정해서 약속함")
        )
    if PHONE.search(text) or EMAIL.search(text):
        findings.append(
            Finding(
                code="pii_in_reply", level="error", message="전화번호·이메일이 답변에 들어 있음"
            )
        )
    if len(text) > MAX_LENGTH:
        findings.append(Finding(code="too_long", level="warning", message="답변이 너무 김"))
    return Validation(passed=not any(f.level == "error" for f in findings), findings=findings)
