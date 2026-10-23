"""답변 가드와 개인정보 마스킹. 막아야 할 문장과 통과해야 할 문장을 함께 둔다 (오탐·미탐)."""

import pytest

from support_agent import guard

# ---------------------------------------------------------------- 환불·보상 약속
MUST_BLOCK = [
    "무조건 환불해 드립니다.",
    "반드시 보상해 드리겠습니다.",
    "환불을 약속드립니다.",
    "전액 환불해 드리겠습니다.",
    "100% 환불 처리해 드립니다.",
    "보상해 드리겠습니다.",
    "환불을 해 드리겠습니다.",
    "쿠폰 5만원을 지급해 드리겠습니다.",
    "5,000원을 입금해 드릴게요.",
    "꼭 환불해 드릴게요.",
    "환불받으실 수 있습니다.",
    "보상을 받으실 수 있어요.",
]
MUST_PASS = [
    "확인 후 안내드리겠습니다.",
    "환불이 가능한지 확인 후 안내드리겠습니다.",
    "환불 기준은 수령 후 7일 이내 단순 변심입니다.",
    "환불 가능 여부는 접수 후 확인되며 결과를 안내드립니다.",
    "환불을 받으실 수 있는지 확인해 보겠습니다.",
    "보상 대상인지 담당자가 검토한 뒤 알려 드리겠습니다.",
    "불량 여부를 확인해 드리겠습니다.",
    "주문 내역을 조회해 드리겠습니다.",
    "배송 현황을 확인해 드리겠습니다.",
    "불편을 드려 죄송합니다. 사진을 보내 주시면 확인하겠습니다.",
]


@pytest.mark.parametrize("text", MUST_BLOCK)
def test_promises_are_blocked(text: str) -> None:
    result = guard.check_reply(text)
    assert not result.passed, text
    assert "promise_refund" in {f.code for f in result.findings}


@pytest.mark.parametrize("text", MUST_PASS)
def test_ordinary_replies_are_not_blocked(text: str) -> None:
    result = guard.check_reply(text)
    assert result.passed, text
    assert result.findings == []


def test_injected_instruction_followed_by_the_model_is_caught() -> None:
    """고객이 \"쿠폰 지급해\"라고 시켜서 모델이 따른 초안이 나와도 가드가 막는다."""
    followed = "알겠습니다. 쿠폰 50000원을 지급해 드리겠습니다."
    assert not guard.check_reply(followed).passed


# ---------------------------------------------------------------- 개인정보
PII_CASES = [
    ("010-1234-5678", "phone"),
    ("010 1234 5678", "phone"),
    ("010.1234.5678", "phone"),
    ("01012345678", "phone"),
    ("+82 10-1234-5678", "phone"),
    ("+821012345678", "phone"),
    ("821012345678", "phone"),
    ("me@example.com", "email"),
    ("900101-1234567", "rrn"),
    ("9001011234567", "rrn"),
    ("4111 1111 1111 1111", "card"),
    ("4111-1111-1111-1111", "card"),
    ("4111111111111111", "card"),
    ("계좌번호 110-123-456789", "account"),
    ("계좌: 1002123456789", "account"),
]


@pytest.mark.parametrize(("text", "kind"), PII_CASES)
def test_pii_is_found_masked_and_blocked_in_replies(text: str, kind: str) -> None:
    sentence = f"연락처는 {text} 입니다"
    assert kind in guard.find_pii(sentence)
    masked, kinds = guard.mask_pii(sentence)
    assert kind in kinds
    digits = "".join(c for c in text if c.isdigit())
    assert digits not in "".join(c for c in masked if c.isdigit()) or kind == "email"
    assert "@" not in masked
    result = guard.check_reply(sentence)
    assert not result.passed and "pii_in_reply" in {f.code for f in result.findings}


NOT_PII = [
    "주문번호 ORD-2026-0101 확인했습니다.",
    "결제 금액은 33,000원이고 배송비는 3,000원입니다.",
    "2026-10-22T01:00:00Z에 접수되었습니다.",
    "2026년 10월 22일 배송 예정입니다.",
    "수령 후 7일 이내에 신청해 주세요.",
    "고양이의 하루 2권, 탁상 달력 1개",
    "4111 1111 1111 1112",  # Luhn 검사를 통과하지 못하는 16자리는 카드번호로 보지 않는다
    "1234567890123",  # 13자리 숫자열이지만 Luhn 검사에 실패한다
    "클레임 번호 12345 / 문의 번호 678",
    "901301-1234567",  # 13월은 생년월일이 아니다
]


@pytest.mark.parametrize("text", NOT_PII)
def test_ordinary_numbers_are_not_masked(text: str) -> None:
    masked, kinds = guard.mask_pii(text)
    assert kinds == [] and masked == text
    assert guard.check_reply(text).passed


def test_mask_keeps_the_rest_of_the_sentence() -> None:
    masked, kinds = guard.mask_pii("제 번호는 010-1234-5678이고 카드는 4111 1111 1111 1111입니다.")
    assert masked == "제 번호는 [전화번호]이고 카드는 [카드번호]입니다."
    assert kinds == ["phone", "card"]


def test_account_number_needs_the_account_word() -> None:
    assert guard.find_pii("110-123-456789") == []  # 단서 없는 숫자열은 건드리지 않는다
    masked, kinds = guard.mask_pii("환불 계좌번호: 110-123-456789 로 부탁드려요")
    assert kinds == ["account"] and "456789" not in masked and "계좌번호: [계좌번호]" in masked


def test_masking_is_idempotent() -> None:
    once, _ = guard.mask_pii("010-1234-5678 / 900101-1234567 / a@b.com")
    twice, kinds = guard.mask_pii(once)
    assert twice == once and kinds == []


# ---------------------------------------------------------------- 링크·길이·빈 답변
def test_links_are_a_warning_not_a_block() -> None:
    result = guard.check_reply("자세한 내용은 https://example.com/help 를 보세요.")
    assert result.passed
    assert [f.code for f in result.findings] == ["link_in_reply"]
    assert guard.check_reply("www.example.com 참고").findings[0].code == "link_in_reply"


def test_empty_and_whitespace_replies_are_blocked() -> None:
    for text in ("", "   ", "\n"):
        result = guard.check_reply(text)
        assert not result.passed and result.findings[0].code == "empty_reply"


def test_findings_accumulate() -> None:
    result = guard.check_reply("전액 환불해 드리겠습니다. 010-1234-5678 https://x.co")
    assert {f.code for f in result.findings} == {"promise_refund", "pii_in_reply", "link_in_reply"}
