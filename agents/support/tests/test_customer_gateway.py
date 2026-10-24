"""고객 화면 대리 호출(shop-api): 본인 문의만 다루고, 내부 분석·초안은 응답에 싣지 않는다."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_agent import handlers, insights
from support_agent.main import app, load_card
from support_agent.schemas import Analysis, Classification, Draft, Validation
from support_agent.store import InquiryStore

INTERNAL_DRAFT = "내부 초안: 사전 승인된 보상 문구"
CUSTOMER_KEYS = {"id", "subject", "status", "order_id", "updated_at"}


def analysis(language: str = "ko", keywords: list[str] | None = None) -> Analysis:
    return Analysis(
        classification=Classification(
            intent_ko="환불 요청",
            category="refund",
            sentiment="frustrated",
            urgency="medium",
            search_query="환불",
            language=language,
            keywords=keywords or ["환불"],
        ),
        drafts=[Draft(style="concise", reply=INTERNAL_DRAFT, validation=Validation(passed=True))],
    )


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> InquiryStore:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    store = InquiryStore()
    for user_id, subject in ((1, "내 배송"), (1, "내 환불"), (2, "남의 환불")):
        store.create(
            context_id="ctx_ui_0001",
            tenant_id="shop-demo",
            user_id=user_id,
            customer_name="김하늘" if user_id == 1 else "이도윤",
            subject=subject,
            order_id="ord_0001",
            order_no=None,
            first_message="환불해 주세요",
            analysis=analysis(),
        )
    monkeypatch.setattr(handlers, "STORE", store)
    return store


def call(
    skill: str,
    input: dict[str, Any],
    requester: str = "shop-api",
    untrusted: dict[str, Any] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_shop_abc123",
            "token": f"mock:{requester}",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input,
        "untrusted": untrusted or {},
    }
    return TestClient(app).post("/a2a", json=body).json()


def test_card_lets_shop_api_read_inquiries_but_not_send_replies() -> None:
    skills = {s.id: s for s in load_card().skills}
    assert "shop-api" in skills["list_inquiries"].allowed_requesters
    assert "shop-api" in skills["get_inquiry"].allowed_requesters
    assert "shop-api" not in skills["send_reply"].allowed_requesters
    denied = call("send_reply", {"inquiry_id": 1, "text": "x"})
    assert denied["status"] == "rejected"


def test_customer_calls_need_a_user_id() -> None:
    for skill, input in (
        ("list_inquiries", {}),
        ("get_inquiry", {"inquiry_id": 1}),
        ("add_customer_message", {"inquiry_id": 1}),
    ):
        result = call(skill, input, untrusted={"message": "안녕하세요"})
        assert result["status"] == "failed" and result["outcome"] == "invalid_input", skill


def test_list_shows_only_the_customers_own_inquiries_in_the_customer_shape() -> None:
    items = call("list_inquiries", {"user_id": 1})["output"]["items"]
    assert [i["subject"] for i in items] == ["내 환불", "내 배송"]  # 최근 순
    assert all(set(i) == CUSTOMER_KEYS | {"preview"} for i in items)
    text = str(items)
    for leaked in ("남의 환불", "이도윤", "김하늘", "frustrated", "refund"):
        assert leaked not in text


def test_get_returns_messages_without_internal_analysis() -> None:
    output = call("get_inquiry", {"inquiry_id": 1, "user_id": 1})["output"]
    assert set(output) == CUSTOMER_KEYS | {"messages"}
    assert [set(m) for m in output["messages"]] == [{"id", "role", "text", "created_at"}]
    assert output["messages"][0]["text"] == "환불해 주세요"
    assert INTERNAL_DRAFT not in str(output) and "analysis" not in output


def test_someone_elses_inquiry_is_not_found_not_forbidden() -> None:
    result = call("get_inquiry", {"inquiry_id": 3, "user_id": 1})
    assert result["outcome"] == "not_found"
    missing = call("get_inquiry", {"inquiry_id": 99, "user_id": 1})
    assert missing["outcome"] == "not_found"  # 없는 문의와 구별되지 않는다
    assert result["output"]["message"].split()[0:1] == missing["output"]["message"].split()[0:1]


def test_a_customer_cannot_add_a_message_to_someone_elses_inquiry(
    _fresh: InquiryStore,
) -> None:
    result = call(
        "add_customer_message", {"inquiry_id": 3, "user_id": 1}, untrusted={"message": "끼어들기"}
    )
    assert result["outcome"] == "not_found"
    assert len(_fresh.get(3).messages) == 1  # 남의 문의는 바뀌지 않았다


def test_own_follow_up_is_added_and_answered_in_the_customer_shape(
    _fresh: InquiryStore,
) -> None:
    result = call(
        "add_customer_message",
        {"inquiry_id": 1, "user_id": 1},
        untrusted={"message": "추가로 문의드려요"},
        key="k-follow",
    )
    assert result["status"] == "completed"
    output = result["output"]
    assert set(output) == CUSTOMER_KEYS | {"messages"}
    assert [m["text"] for m in output["messages"]][-1] == "추가로 문의드려요"
    assert "analysis" not in str(output) and INTERNAL_DRAFT not in str(output)
    assert len(_fresh.get(1).messages) == 2


def test_a_new_inquiry_through_shop_api_returns_no_internal_analysis() -> None:
    result = call(
        "create_inquiry",
        {"user_id": 5, "customer_name": "신규", "order_id": "ord_0009"},
        untrusted={"subject": "환불 문의", "message": "환불되나요"},
        key="k-new",
    )
    output = result["output"]
    assert set(output) == CUSTOMER_KEYS | {"messages"} and output["subject"] == "환불 문의"
    assert "classification" not in str(output) and "drafts" not in str(output)
    replay = call(
        "create_inquiry",
        {"user_id": 5, "customer_name": "신규", "order_id": "ord_0009"},
        untrusted={"subject": "환불 문의", "message": "환불되나요"},
        key="k-new",
    )
    assert replay["output"] == output  # 같은 요청의 재시도도 고객용 모양


def test_staff_requests_are_unchanged() -> None:
    staff = call("get_inquiry", {"inquiry_id": 3}, requester="dashboard-api")["output"]
    assert staff["customer_name"] == "이도윤" and staff["messages"][0]["analysis"]
    everyone = call("list_inquiries", {}, requester="dashboard-api")["output"]["items"]
    assert len(everyone) == 3
    only_user_2 = call("list_inquiries", {"user_id": 2}, requester="dashboard-api")["output"][
        "items"
    ]
    assert [i["subject"] for i in only_user_2] == ["남의 환불"]
    assert "customer_name" in only_user_2[0]  # 상담원은 고객 이름·분류를 본다


# ---------------------------------------------------------------- 키워드·언어·감정 집계
def test_kpis_count_languages_sentiments_and_top_keywords(_fresh: InquiryStore) -> None:
    store = InquiryStore()
    spec = [
        ("ko", ["환불", "불량"]),
        ("en", ["Refund", "damaged"]),
        ("en", ["refund"]),
        ("ja", ["返金"]),
    ]
    for n, (language, keywords) in enumerate(spec, start=1):
        store.create(
            context_id=f"ctx_ui_{n:04d}",
            tenant_id="shop-demo",
            user_id=n,
            customer_name="고객",
            subject="문의",
            order_id=None,
            order_no=None,
            first_message="m",
            analysis=analysis(language, keywords),
        )
    kpis = insights.kpis(store)
    assert kpis.by_language == {"ko": 1, "en": 2, "ja": 1}
    assert kpis.by_sentiment == {"frustrated": 4}
    top = [(k.keyword, k.count) for k in kpis.top_keywords]
    assert top[0] == ("refund", 2)  # 대소문자를 합쳐 센다
    assert set(top) >= {("환불", 1), ("불량", 1), ("damaged", 1), ("返金", 1)}
    assert len(top) <= 10


def test_kpis_of_an_empty_store_have_empty_trends() -> None:
    kpis = insights.kpis(InquiryStore())
    assert (kpis.by_language, kpis.by_sentiment, kpis.top_keywords) == ({}, {}, [])
