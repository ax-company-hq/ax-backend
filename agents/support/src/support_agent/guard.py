"""답변 가드: 고객에게 나가는 답변을 코드 규칙으로 검사한다. LLM을 쓰지 않는다.

`error`가 하나라도 있으면 통과하지 못한다(`passed=False`). 경고(`warning`)는 통과를 막지 않는다.

규칙 기반이라 오탐(정상 답변을 막음)과 미탐(약속·개인정보를 놓침)이 모두 생길 수 있다.
그래서 막아야 할
문장과 통과해야 할 문장을 함께 `tests/test_guard.py`에 둔다. 규칙을 바꿀 때 양쪽을 같이 본다.
"""

from __future__ import annotations

import re

from support_agent import languages
from support_agent.schemas import Finding, Validation

# ---------------------------------------------------------------- 개인정보
# 국내 휴대전화: 010-1234-5678, 010 1234 5678, 01012345678, 국제 표기 +82 10-1234-5678, 821012345678
PHONE = re.compile(
    r"(?<!\d)(?:(?:\+82|82)[-\s.]?0?1[016789]|01[016789])[-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)"
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 주민등록번호: 생년월일 6자리(월·일 범위 확인) + 성별 1~4 + 6자리
RRN = re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])[-\s]?[1-4]\d{6}(?!\d)")
# 카드번호 후보: 13~19자리(공백·하이픈 허용). 실제 카드번호만 가리도록 Luhn 검사를 통과해야 한다
CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
# 계좌번호: 은행마다 형식이 달라서 "계좌" 같은 단서 뒤의 숫자열만 본다
ACCOUNT = re.compile(r"(계좌(?:\s?번호)?\s*[:：]?\s*)(\d[\d-]{7,18}\d)")

# ---------------------------------------------------------------- 약속·단정
# 환불·보상을 단정하거나 약속하는 표현. 조건·확인 없이 해 준다고 말하면 막는다.
PROMISE = re.compile(
    "|".join(
        [
            # 환불은 약속/보장/확정/무조건/반드시 같은 단정어와 함께 쓰인 경우
            r"(환불|보상|배상).{0,12}(약속|보장|확정|무조건|반드시)",
            r"(무조건|반드시).{0,8}(환불|보상|배상)",
            # 환불해 드리겠습니다, 보상 처리해 드립니다, 쿠폰을 지급해 드릴게요
            r"(환불|보상|배상|지급|입금)(을|를)?\s?(처리\s?)?해\s?(드리겠|드립니다|드릴게|드리도록|드릴\s?것)",
            # 꼭 환불, 100% 환불
            r"(꼭|100\s?%)\s?(환불|보상|배상)",
            # 환불받으실 수 있습니다 (가능 여부 확인 중이라는 말은 "있는지"라서 걸리지 않는다)
            r"(환불|보상|배상)(을)?\s?받(으실|으시|을)\s?수\s?있(습니다|어요|으세요)",
        ]
    )
)
# 영어·일본어·중국어 약속 표현. 지원 언어(`languages.SUPPORTED`)마다 같은 뜻의 단정·약속을 막는다.
PROMISE_EN = re.compile(
    "|".join(
        [
            r"\b(refunds?|compensation|reimbursement)\b.{0,20}\b(guarantee\w*|promise\w*|definitely|certainly|absolutely)\b",
            r"\b(guarantee\w*|promise\w*|definitely|certainly|absolutely)\b.{0,20}\b(refund|compensat\w*|reimburs\w*)",
            r"\b(we|i)\s?(will|'ll|shall)\s+(definitely\s+|certainly\s+|surely\s+)?(refund|reimburse|compensate|credit)\b",
            r"\byou\s?(will|'ll)\s+(definitely\s+|certainly\s+)?(get|receive)\s+(a\s+|your\s+)?(full\s+)?(refund|compensation)\b",
            r"\b100\s?%\s+(refund|compensation)",
        ]
    ),
    re.IGNORECASE,
)
PROMISE_JA = re.compile(
    "|".join(
        [
            r"(返金|補償|賠償).{0,8}(お約束|保証|確約|必ず|間違いなく)",
            r"(必ず|絶対に|間違いなく).{0,8}(返金|補償|賠償)",
            r"(返金|補償|賠償)(を)?(いたします|致します|します|させていただきます)",
        ]
    )
)
PROMISE_ZH = re.compile(
    "|".join(
        [
            r"(退款|退費|赔偿|賠償|补偿|補償).{0,8}(保证|保證|一定|承诺|承諾|确保|確保|肯定)",
            r"(一定|保证|保證|肯定|承诺|承諾|确保|確保).{0,6}(退款|退費|赔偿|賠償|补偿|補償)",
            r"(会|會|将|將)(为您|為您|给您|給您)?(退款|退費|赔偿|賠償|补偿|補償)",
        ]
    )
)
LINK = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
MAX_LENGTH = 1200


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        n = int(char)
        if index % 2 == 1:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def _card_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            spans.append(match.span())
    return spans


def find_pii(text: str) -> list[str]:
    """들어 있는 개인정보 종류 (`phone`, `email`, `rrn`, `card`, `account`)."""
    kinds = []
    if PHONE.search(text):
        kinds.append("phone")
    if EMAIL.search(text):
        kinds.append("email")
    if RRN.search(text):
        kinds.append("rrn")
    if _card_spans(text):
        kinds.append("card")
    if ACCOUNT.search(text):
        kinds.append("account")
    return kinds


def mask_pii(text: str) -> tuple[str, list[str]]:
    """개인정보를 가리고, 가린 종류를 함께 돌려준다 (LLM에 넘기기 전에 쓴다)."""
    kinds = find_pii(text)
    # 카드번호를 먼저 가린다 (전화번호 규칙이 카드번호 일부를 먼저 먹지 않게)
    for start, end in reversed(_card_spans(text)):
        text = text[:start] + "[카드번호]" + text[end:]
    text = RRN.sub("[주민등록번호]", text)
    text = PHONE.sub("[전화번호]", text)
    text = EMAIL.sub("[이메일]", text)
    text = ACCOUNT.sub(lambda m: m.group(1) + "[계좌번호]", text)
    return text, kinds


def check_reply(text: str) -> Validation:
    findings: list[Finding] = []
    if not text.strip():
        findings.append(Finding(code="empty_reply", level="error", message="답변이 비어 있다"))
    language = languages.detect(text)
    if language == "other":  # 가드가 약속 표현을 검사하지 못하는 문자는 내보내지 않는다
        findings.append(
            Finding(
                code="unsupported_language",
                level="error",
                message="자동 검사를 지원하지 않는 언어(한국어·영어·일본어·중국어만 가능)",
            )
        )
    elif language == "en" and languages.ACCENTED_LATIN.search(text):
        findings.append(
            Finding(
                code="language_not_verified",
                level="warning",
                message="영어 외 라틴 문자 언어일 수 있어 약속 표현을 검사하지 못함, 직접 확인",
            )
        )
    if any(p.search(text) for p in (PROMISE, PROMISE_EN, PROMISE_JA, PROMISE_ZH)):
        findings.append(
            Finding(code="promise_refund", level="error", message="환불·보상을 단정해서 약속함")
        )
    if find_pii(text):
        findings.append(
            Finding(code="pii_in_reply", level="error", message="개인정보가 답변에 들어 있음")
        )
    if LINK.search(text):
        findings.append(
            Finding(code="link_in_reply", level="warning", message="외부 링크가 답변에 들어 있음")
        )
    if len(text) > MAX_LENGTH:
        findings.append(Finding(code="too_long", level="warning", message="답변이 너무 김"))
    return Validation(passed=not any(f.level == "error" for f in findings), findings=findings)
