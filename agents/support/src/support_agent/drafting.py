"""고객 메시지 분석(분류, 근거 검색, 답변 초안 3가지)과 대화 요약.

- 고객 원문은 PII를 가린 뒤 `untrusted` 데이터 블록으로만 LLM에 넣는다. 지시로 해석하지 않는다.
- LLM 응답은 pydantic 스키마로 검증한다. 실패하면 고정 템플릿으로 대체하고
  `fallback=True`를 돌려준다.
- 근거 검색 점수·가드 검사·건수는 코드가 계산한다. LLM은 분류와 문장만 맡는다.
- 모델 이름·엔드포인트는 `.env`(`SUPPORT_LLM_MODEL` 또는 `LLM_MODEL`)에서 읽는다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ax_agent_template.config import is_mock
from ax_agent_template.llm import LLMClient, MockLLMClient
from support_agent import guard, kb, languages
from support_agent.schemas import (
    STYLES,
    Analysis,
    Classification,
    Conversation,
    Draft,
    DraftStyle,
    LLMClassification,
    LLMDrafts,
    Source,
    StyleReply,
    Summary,
)

logger = logging.getLogger("ax.support.drafting")

_MOCK_CLASSIFICATION = LLMClassification(
    intent_ko="환불 요청",
    category="refund",
    sentiment="frustrated",
    urgency="medium",
    search_query="환불 불량",
    keywords=["환불", "불량"],
)
_MOCK_DRAFTS = LLMDrafts(
    replies=[
        StyleReply(
            style="empathetic", reply="불편을 드려 죄송합니다. 내용을 확인한 뒤 안내드리겠습니다."
        ),
        StyleReply(style="concise", reply="접수되었습니다. 확인 후 안내드립니다."),
        StyleReply(
            style="step_by_step", reply="접수, 확인, 처리 순서로 진행하며 단계마다 안내드립니다."
        ),
    ]
)
_MOCK_SUMMARY = Summary(
    summary_ko="고객이 환불을 요청했다.",
    issue_ko="환불 요청",
    actions_ko=["문의 접수"],
    next_action_ko="상담원 확인",
    category="refund",
    resolved=False,
    escalate=False,
    escalate_reason_ko="",
    tags=["refund"],
)

# LLM을 쓸 수 없을 때(모델 미설정·스키마 검증 실패·호출 오류) 쓰는 고정 템플릿
_FALLBACK_REPLIES: dict[DraftStyle, str] = {
    "empathetic": "불편을 드려 죄송합니다. 문의하신 내용을 확인한 뒤 빠르게 안내드리겠습니다.",
    "concise": "문의가 접수되었습니다. 확인 후 안내드리겠습니다.",
    "step_by_step": "문의 접수, 내용 확인, 처리 안내 순서로 진행하며 단계마다 말씀드리겠습니다.",
}
# 언어별 대체 템플릿. 환불·보상을 약속하지 않는 문장만 둔다 (가드 통과를 테스트가 확인한다).
_FALLBACK_BY_LANGUAGE: dict[str, dict[DraftStyle, str]] = {
    "ko": _FALLBACK_REPLIES,
    "en": {
        "empathetic": "We are sorry for the inconvenience. We will review your inquiry and "
        "get back to you shortly.",
        "concise": "Your inquiry has been received. We will review it and follow up.",
        "step_by_step": "We will receive your inquiry, review the details, and explain the next "
        "steps at each stage.",
    },
    "ja": {
        "empathetic": "ご不便をおかけし申し訳ございません。"
        "内容を確認のうえ、改めてご案内いたします。",
        "concise": "お問い合わせを受け付けました。確認後にご案内いたします。",
        "step_by_step": "受付、内容の確認、ご案内の順で進め、各段階でご連絡いたします。",
    },
    "zh": {
        "empathetic": "给您带来不便，我们深表歉意。我们会先确认您咨询的内容，再尽快与您联系。",
        "concise": "已收到您的咨询，确认后会与您联系。",
        "step_by_step": "我们会按受理、确认内容、告知后续步骤的顺序处理，并在每个阶段与您联系。",
    },
}
_CATEGORY_KEYWORDS = {
    "refund": (
        "환불", "반품", "교환", "불량", "찢어", "파손",
        "refund", "return", "exchange", "broken", "damaged", "defect",
        "返金", "返品", "交換", "不良", "破損",
        "退款", "退货", "换货", "损坏", "破损",
    ),
    "shipping": (
        "배송", "택배", "도착", "언제",
        "shipping", "delivery", "arrive", "tracking",
        "配送", "届", "追跡",
        "快递", "物流", "配送", "到货",
    ),
}  # fmt: skip
# 근거 문서(한국어)를 찾기 위한 분류별 대체 검색어. 한국어가 아닌 문의의 대체 경로에서만 쓴다.
_CATEGORY_QUERY = {"refund": "환불 교환 반품", "shipping": "배송 지연 도착"}
MAX_KEYWORDS = 5
MAX_KEYWORD_LENGTH = 30


def get_llm() -> LLMClient:
    """목 모드(CI)는 `MockLLMClient`, 그 밖에는 설정된 모델로 `LLMClient("support")`다."""
    if is_mock("support"):
        return MockLLMClient(
            responses={
                LLMClassification: _MOCK_CLASSIFICATION,
                LLMDrafts: _MOCK_DRAFTS,
                Summary: _MOCK_SUMMARY,
            }
        )
    return LLMClient("support")


def clean_keywords(values: list[str]) -> list[str]:
    """LLM이 고른 키워드를 코드로 거른다: 개인정보 제거, 빈 값·긴 문장 제외, 중복 제거, 최대 5개."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        word = str(value).strip()
        if not word or len(word) > MAX_KEYWORD_LENGTH or guard.find_pii(word):
            continue  # 개인정보가 든 키워드는 가리지 않고 버린다 (마스킹 표시가 키워드가 된다)
        key = word.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(word)
    return cleaned[:MAX_KEYWORDS]


def keyword_classification(text: str) -> LLMClassification:
    """LLM 없이 키워드로 분류한다 (대체 경로)."""
    lowered = text.casefold()
    hits = {
        name: [w for w in words if w.casefold() in lowered]
        for name, words in _CATEGORY_KEYWORDS.items()
    }
    category = next((name for name, found in hits.items() if found), "general")
    korean = languages.detect(text) == "ko"
    return LLMClassification(
        intent_ko="문의",
        category=category,
        sentiment="calm",
        urgency="medium",
        search_query=text[:80] if korean else _CATEGORY_QUERY.get(category, ""),
        keywords=clean_keywords(hits.get(category, [])),
    )


def _validated_drafts(
    replies: list[StyleReply], sources: list[Source], language: str = "ko"
) -> list[Draft]:
    """초안 3가지를 만들고 가드로 검사한다. 빠진 스타일은 `language`의 대체 템플릿으로 채운다."""
    by_style = {r.style: r for r in replies}
    templates = _FALLBACK_BY_LANGUAGE[language]
    source_ids = [s.id for s in sources]
    drafts = []
    for style in STYLES:
        given = by_style.get(style)
        text = given.reply if given and given.reply.strip() else templates[style]
        gloss = given.reply_ko if given and given.reply.strip() else _FALLBACK_REPLIES[style]
        drafts.append(
            Draft(
                style=style,
                reply=text,
                source_ids=source_ids,
                validation=guard.check_reply(text),
                language=language,
                reply_ko=None if language == "ko" else (gloss or None),
            )
        )
    return drafts


async def analyze(
    text: str, subject: str = "", extra_sources: list[Source] | None = None
) -> tuple[Analysis, bool]:
    """고객 메시지를 분석한다. `(분석 결과, 대체 템플릿 사용 여부)`를 돌려준다.

    `extra_sources`(예: 마케팅 공지)는 정책 검색 결과 앞에 붙는다.
    """
    started = time.perf_counter()
    masked, pii_kinds = guard.mask_pii(text)
    # 언어는 코드가 문자 종류로 판별한다. 답변은 가드가 검사할 수 있는 언어(ko·en·ja·zh)로만 쓴다
    language = languages.detect(f"{subject} {text}")
    reply_language = languages.reply_language(language)
    untrusted: dict[str, Any] = {"subject": guard.mask_pii(subject)[0], "customer_message": masked}
    llm = get_llm()
    fallback = False

    try:
        classified = await llm.structured(
            "고객 메시지의 의도, 분야, 감정, 긴급도를 분류하고 정책 문서 검색 질의를 만들어라. "
            "정책 문서는 한국어이므로 search_query는 고객 언어와 관계없이 한국어로 쓴다. "
            "keywords에는 고객 메시지의 핵심 단어를 최대 5개 넣고 개인정보는 넣지 않는다.",
            LLMClassification,
            untrusted=untrusted,
        )
    except Exception:
        logger.warning("분류 LLM 실패, 키워드 분류로 대체한다", exc_info=True)
        classified = keyword_classification(masked)
        fallback = True

    classified_data = classified.model_dump()
    keywords = clean_keywords(classified_data.pop("keywords", []))
    if not keywords:  # LLM이 키워드를 못 줬으면 규칙으로 찾은 것을 쓴다
        keywords = keyword_classification(masked).keywords
    classification = Classification(
        **classified_data, pii_masked=pii_kinds, language=language, keywords=keywords
    )
    sources = [*(extra_sources or []), *kb.search(classification.search_query or masked)]
    classify_ms = int((time.perf_counter() - started) * 1000)

    drafting_started = time.perf_counter()
    try:
        policy = "\n".join(f"[{s.id}] {s.title} {s.section}: {s.text}" for s in sources)
        drafted = await llm.structured(
            "아래 정책 문서에 근거해 고객 답변 초안을 empathetic, concise, step_by_step 세 가지 "
            "스타일로 쓴다. 환불·보상을 약속하지 않고 개인정보를 적지 않는다. "
            f"reply는 {languages.NAMES[reply_language]}로 쓴다. 한국어가 아니면 상담원이 읽을 "
            "한국어 번역을 reply_ko에 함께 넣는다.\n\n"
            f"정책 문서:\n{policy or '(근거 없음)'}",
            LLMDrafts,
            untrusted=untrusted,
        )
        replies = drafted.replies
    except Exception:
        logger.warning("초안 LLM 실패, 고정 템플릿으로 대체한다", exc_info=True)
        replies = []
        fallback = True

    drafts = _validated_drafts(replies, sources, reply_language)
    analysis = Analysis(
        classification=classification,
        sources=sources,
        drafts=drafts,
        timings_ms={
            "classify": classify_ms,
            "draft": int((time.perf_counter() - drafting_started) * 1000),
        },
        validation=guard.check_reply(masked),
    )
    return analysis, fallback


async def summarize(conversation: Conversation) -> tuple[Summary, bool]:
    """대화를 요약한다. 실패하면 고정 템플릿으로 대체한다."""
    transcript = [
        {"role": m.role, "text": guard.mask_pii(m.text)[0]} for m in conversation.messages
    ]
    try:
        summary = await get_llm().structured(
            "상담 대화를 한국어로 요약하고, 해결 여부와 사람에게 넘길 필요를 판단하라.",
            Summary,
            untrusted={"subject": conversation.subject, "transcript": transcript},
        )
        return summary, False
    except Exception:
        logger.warning("요약 LLM 실패, 고정 템플릿으로 대체한다", exc_info=True)
        return (
            Summary(
                summary_ko=f"{conversation.subject} 문의 (메시지 {len(conversation.messages)}건)",
                issue_ko=conversation.subject,
                actions_ko=[],
                next_action_ko="상담원 확인",
                category=conversation.category or "general",
                resolved=conversation.status == "resolved",
                escalate=conversation.status == "escalated",
                escalate_reason_ko="",
                tags=[conversation.category] if conversation.category else [],
            ),
            True,
        )
