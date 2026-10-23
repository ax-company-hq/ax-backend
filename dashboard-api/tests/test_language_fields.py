def test_language_and_keywords_pass_through_to_the_screen_models() -> None:
    """support 분석의 새 필드(언어, 키워드, 한국어 번역)가 화면 모델에서 잘리지 않는다."""
    from ax_dashboard_api.schemas import Analysis

    analysis = Analysis.model_validate(
        {
            "classification": {
                "intent_ko": "환불 요청",
                "category": "refund",
                "sentiment": "calm",
                "urgency": "medium",
                "search_query": "환불",
                "language": "en",
                "keywords": ["refund", "damaged"],
            },
            "drafts": [
                {
                    "style": "concise",
                    "reply": "We will review it.",
                    "validation": {"passed": True, "findings": []},
                    "language": "en",
                    "reply_ko": "확인하겠습니다.",
                }
            ],
        }
    )
    dumped = analysis.model_dump(mode="json")
    assert dumped["classification"]["language"] == "en"
    assert dumped["classification"]["keywords"] == ["refund", "damaged"]
    assert dumped["drafts"][0]["language"] == "en"
    assert dumped["drafts"][0]["reply_ko"] == "확인하겠습니다."
    old = Analysis.model_validate(
        {
            "classification": {
                "intent_ko": "문의",
                "category": "general",
                "sentiment": "calm",
                "urgency": "low",
                "search_query": "",
            }
        }
    )
    assert old.classification.language == "ko" and old.classification.keywords == []
