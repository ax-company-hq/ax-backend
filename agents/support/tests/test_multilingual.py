"""다국어 문의: 언어 판별, 약속 표현 가드(ko·en·ja·zh), 키워드 정리, 고객 언어 초안."""

import asyncio
from typing import Any

import pytest

from ax_agent_template.llm import MockLLMClient
from support_agent import drafting, guard, languages
from support_agent.schemas import LLMClassification, LLMDrafts, StyleReply, Summary

STYLES = ("empathetic", "concise", "step_by_step")


@pytest.fixture(autouse=True)
def _mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")


# ---------------------------------------------------------------- 언어 판별
@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("책이 찢어져서 왔어요", "ko"),
        ("My book arrived damaged", "en"),
        ("本が破れて届きました。返金できますか", "ja"),  # 가나가 있으면 한자가 섞여도 일본어
        ("书到的时候是破的，可以退款吗", "zh"),
        ("Книга пришла порванной", "other"),
        ("1234 5678 !!!", "ko"),  # 글자가 없으면 기본값
        ("환불 refund 문의합니다", "ko"),  # 한국어 문장에 섞인 영어는 한국어
    ],
)
def test_language_is_detected_from_scripts(text: str, language: str) -> None:
    assert languages.detect(text) == language


def test_unsupported_languages_are_answered_in_english() -> None:
    assert languages.reply_language("ko") == "ko"
    assert languages.reply_language("ja") == "ja"
    assert languages.reply_language("other") == "en"


# ---------------------------------------------------------------- 가드
@pytest.mark.parametrize(
    "text",
    [
        "We will definitely refund your order.",
        "I'll refund you today.",
        "A full refund is guaranteed for you.",
        "You will get a full refund.",
        "返金を必ずお約束します。",
        "返金いたします。",
        "我们保证退款。",
        "我们一定会退款给您。",
        "환불해 드리겠습니다",
    ],
)
def test_promises_are_blocked_in_every_supported_language(text: str) -> None:
    result = guard.check_reply(text)
    assert not result.passed and "promise_refund" in [f.code for f in result.findings]


@pytest.mark.parametrize(
    "text",
    [
        "We will check your order and get back to you.",
        "We are looking into the refund policy for your case.",
        "Refunds are processed after we inspect the item.",
        "ご注文を確認して、改めてご連絡いたします。",
        "我们会尽快确认您的订单。",
        "확인 후 안내드리겠습니다",
    ],
)
def test_ordinary_replies_pass_in_every_supported_language(text: str) -> None:
    assert guard.check_reply(text).passed


def test_languages_the_guard_cannot_check_are_blocked() -> None:
    result = guard.check_reply("Спасибо за обращение, мы вернём деньги")
    assert not result.passed and [f.code for f in result.findings] == ["unsupported_language"]


def test_latin_languages_other_than_english_are_flagged_not_blocked() -> None:
    result = guard.check_reply("¿Dónde está mi pedido? Lo revisaremos y le responderemos pronto.")
    assert result.passed
    assert "language_not_verified" in [f.code for f in result.findings]


def test_personal_data_is_still_blocked_in_other_languages() -> None:
    result = guard.check_reply("Please call me back: 010-1234-5678")
    assert not result.passed and "pii_in_reply" in [f.code for f in result.findings]


@pytest.mark.parametrize("language", ["ko", "en", "ja", "zh"])
def test_every_fallback_template_passes_the_guard(language: str) -> None:
    templates = drafting._FALLBACK_BY_LANGUAGE[language]
    assert set(templates) == set(STYLES)
    for style, reply in templates.items():
        assert guard.check_reply(reply).passed, (language, style)
        assert languages.detect(reply) == language, (language, style)


# ---------------------------------------------------------------- 키워드
def test_keywords_are_cleaned_by_code() -> None:
    cleaned = drafting.clean_keywords(
        [
            " 환불 ",
            "환불",  # 중복
            "Refund",
            "refund",  # 대소문자만 다른 중복
            "010-1234-5678",  # 개인정보
            "",
            "아주 긴 문장은 키워드가 아니다. 아주 긴 문장은 키워드가 아니다. 아주 긴 문장",
            "배송",
            "불량",
            "찢어짐",
            "일곱번째",
        ]
    )
    assert cleaned == ["환불", "Refund", "배송", "불량", "찢어짐"]  # 최대 5개


def test_keyword_fallback_works_without_an_llm_in_english() -> None:
    result = drafting.keyword_classification("My book arrived damaged and I want a refund")
    assert result.category == "refund"
    assert result.search_query == "환불 교환 반품"  # 근거 문서가 한국어라 한국어로 찾는다
    assert {"refund", "damaged"} <= set(result.keywords)


# ---------------------------------------------------------------- 고객 언어 초안
ENGLISH = LLMDrafts(
    replies=[
        StyleReply(
            style="empathetic",
            reply="We are sorry about the damage. We will inspect it and follow up.",
            reply_ko="파손되어 죄송합니다. 확인한 뒤 안내드리겠습니다.",
        ),
        StyleReply(
            style="concise",
            reply="We will definitely refund your order.",  # 가드가 막아야 한다
            reply_ko="반드시 환불해 드립니다.",
        ),
        StyleReply(
            style="step_by_step",
            reply="We will review the photos, check the order, and update you.",
            reply_ko="사진과 주문을 확인한 뒤 알려 드립니다.",
        ),
    ]
)


def fake_llm(drafts: LLMDrafts, keywords: list[str] | None = None) -> MockLLMClient:
    classification = LLMClassification(
        intent_ko="파손 환불 요청",
        category="refund",
        sentiment="frustrated",
        urgency="medium",
        search_query="환불 불량",
        keywords=keywords if keywords is not None else ["refund", "damaged", "010-1234-5678"],
    )
    summary = Summary(
        summary_ko="s",
        issue_ko="i",
        actions_ko=[],
        next_action_ko="n",
        category="refund",
        resolved=False,
        escalate=False,
        escalate_reason_ko="",
        tags=[],
    )
    return MockLLMClient(
        responses={LLMClassification: classification, LLMDrafts: drafts, Summary: summary}
    )


def analyze(text: str) -> tuple[Any, bool]:
    return asyncio.run(drafting.analyze(text))


def test_english_inquiry_gets_english_drafts_with_korean_gloss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafting, "get_llm", lambda: fake_llm(ENGLISH))
    analysis, fallback = analyze("My book arrived damaged. Can I get a refund? 010-1234-5678")
    assert not fallback
    c = analysis.classification
    assert c.language == "en" and c.keywords == ["refund", "damaged"]  # 개인정보 키워드는 제거
    assert c.search_query == "환불 불량"  # 근거 검색은 한국어
    by_style = {d.style: d for d in analysis.drafts}
    assert all(d.language == "en" for d in analysis.drafts)
    assert by_style["empathetic"].reply_ko == "파손되어 죄송합니다. 확인한 뒤 안내드리겠습니다."
    assert by_style["empathetic"].validation.passed
    promise = by_style["concise"].validation  # 영어 약속은 가드가 막는다
    assert not promise.passed and "promise_refund" in [f.code for f in promise.findings]


def test_japanese_inquiry_without_an_llm_uses_japanese_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Down:
        async def structured(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("llm down")

    monkeypatch.setattr(drafting, "get_llm", lambda: Down())
    analysis, fallback = analyze("本が破れて届きました。返金について教えてください")
    assert fallback and analysis.classification.language == "ja"
    assert analysis.classification.category == "refund"
    for draft in analysis.drafts:
        assert draft.language == "ja" and languages.detect(draft.reply) == "ja"
        assert draft.validation.passed
        assert draft.reply_ko  # 상담원이 읽을 한국어 번역


def test_unsupported_language_is_answered_in_english(monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        async def structured(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("llm down")

    monkeypatch.setattr(drafting, "get_llm", lambda: Down())
    analysis, _ = analyze("Книга пришла порванной, хочу вернуть деньги")
    assert analysis.classification.language == "other"
    assert all(d.language == "en" and d.validation.passed for d in analysis.drafts)


def test_korean_inquiry_keeps_the_existing_behavior() -> None:
    analysis, fallback = analyze("책이 찢어져서 왔어요. 환불되나요?")
    assert not fallback
    assert analysis.classification.language == "ko"
    assert analysis.classification.keywords == ["환불", "불량"]
    assert all(d.language == "ko" and d.reply_ko is None for d in analysis.drafts)


def test_old_stored_analyses_without_the_new_fields_are_still_read() -> None:
    from support_agent.schemas import Classification, Draft, Validation

    old = Classification.model_validate(
        {
            "intent_ko": "환불 요청",
            "category": "refund",
            "sentiment": "calm",
            "urgency": "medium",
            "search_query": "환불",
        }
    )
    assert (old.language, old.keywords) == ("ko", [])
    draft = Draft(
        style="concise", reply="확인 후 안내드립니다.", validation=Validation(passed=True)
    )
    assert (draft.language, draft.reply_ko) == ("ko", None)
