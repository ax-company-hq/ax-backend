"""knowledge_gap_report: 분류별 지식 공백 순위 (읽기 전용, 코드가 센다). 설계 #441 1단계."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_agent import gap_rules, handlers
from support_agent.main import ALL_HANDLERS, app, load_card
from support_agent.schemas import Analysis, Classification, Draft, Finding, Source, Validation
from support_agent.store import InquiryStore

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SOURCE = Source(
    id="refund-1",
    doc_id="refund",
    category="refund",
    title="환불",
    section="1",
    text="x",
    score=0.8,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())


def analysis(category: str, with_source: bool, blocked: bool = False) -> Analysis:
    findings = [Finding(code="promise_refund", level="error", message="x")] if blocked else []
    return Analysis(
        classification=Classification(
            intent_ko="문의",
            category=category,
            sentiment="calm",
            urgency="medium",
            search_query=category,
        ),
        sources=[SOURCE] if with_source else [],
        drafts=[
            Draft(
                style="concise",
                reply="답변",
                validation=Validation(passed=not blocked, findings=findings),
            )
        ],
    )


def add(store: InquiryStore, category: str, with_source: bool, days_ago: float = 0.5) -> int:
    conversation = store.create(
        context_id="ctx_req_0001",
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="문의",
        order_id=None,
        order_no=None,
        first_message="질문입니다",
        analysis=analysis(category, with_source),
    )
    stamp = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conversation.messages[0].created_at = stamp
    conversation.updated_at = stamp
    return conversation.id


def event(inquiry_id: int, result: str, **extra: Any) -> dict[str, Any]:
    return {
        "context_id": "ctx_req_0001",
        "inquiry_id": inquiry_id,
        "style": "concise",
        "result": result,
        "occurred_at": (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **extra,
    }


def item(report: Any, category: str) -> Any:
    return next(i for i in report.categories if i.category == category)


def test_no_evidence_category_is_a_no_document_gap() -> None:
    store = InquiryStore()
    for _ in range(5):
        add(store, "coupon", with_source=False)
    for _ in range(5):
        add(store, "refund", with_source=True)
    report = gap_rules.build_report(store, NOW)
    coupon = item(report, "coupon")
    assert (coupon.no_evidence, coupon.analyzed_messages, coupon.no_evidence_rate) == (5, 5, 1.0)
    assert (coupon.gap_type, coupon.suggested_action) == ("no_document", "add_document")
    assert coupon.rank == 1
    refund = item(report, "refund")
    assert (refund.gap_type, refund.no_evidence_rate, refund.rank) == ("none", 0.0, 2)


def test_rejections_make_a_weak_document_and_reasons_are_counted() -> None:
    store = InquiryStore()
    ids = [add(store, "refund", with_source=True) for _ in range(5)]
    store.draft_events.append(event(ids[0], "rejected", reason_code="missing_policy"))
    store.draft_events.append(event(ids[1], "rejected", reason_code="tone"))
    store.draft_events.append(event(ids[2], "blocked"))
    store.draft_events.append(event(ids[3], "accepted"))
    report = gap_rules.build_report(store, NOW)
    refund = item(report, "refund")
    assert (refund.drafts_rejected, refund.drafts_blocked, refund.drafts_decided) == (2, 1, 4)
    assert refund.reject_rate == 0.75
    assert refund.reject_reasons == {"missing_policy": 1, "tone": 1}
    assert (refund.gap_type, refund.suggested_action) == ("weak_document", "review_document")
    assert [d.doc_id for d in report.documents] == ["refund"]


def test_small_samples_get_no_rank_and_zero_denominators_are_none() -> None:
    store = InquiryStore()
    add(store, "shipping", with_source=False)
    shipping = item(gap_rules.build_report(store, NOW), "shipping")
    assert (shipping.gap_type, shipping.rank, shipping.suggested_action) == (
        "insufficient_samples",
        None,
        "none",
    )
    assert shipping.reject_rate is None  # 결정한 초안이 0건이면 0이 아니라 None


def test_old_events_without_fields_are_joined_by_inquiry_and_window_is_respected() -> None:
    store = InquiryStore()
    ids = [add(store, "refund", with_source=True) for _ in range(5)]
    old = add(store, "refund", with_source=True, days_ago=20)
    legacy = {"context_id": "c", "inquiry_id": ids[0], "style": "concise", "result": "rejected"}
    store.draft_events.append(legacy)  # category·occurred_at 없음
    store.draft_events.append({**legacy, "inquiry_id": old})  # 기간 밖 문의에 이은 옛 기록
    refund = item(gap_rules.build_report(store, NOW), "refund")
    assert (refund.analyzed_messages, refund.drafts_rejected) == (5, 1)


def test_escalated_inquiries_raise_the_score_and_category_filter_applies() -> None:
    store = InquiryStore()
    ids = [add(store, "refund", with_source=True) for _ in range(5)]
    store.set_status(ids[0], "escalated")
    for _ in range(5):
        add(store, "shipping", with_source=True)
    store.draft_events.append(event(ids[0], "accepted"))
    report = gap_rules.build_report(store, NOW)
    refund = item(report, "refund")
    assert refund.escalated == 1 and refund.escalated_rate == 0.2
    assert refund.rank == 1  # 이관이 있는 쪽이 앞선다
    only = gap_rules.build_report(store, NOW, category="shipping")
    assert [i.category for i in only.categories] == ["shipping"]


def test_report_is_read_only_has_no_text_and_carries_seed_note() -> None:
    store = InquiryStore()
    for _ in range(5):
        add(store, "refund", with_source=False)
    before = store.all()[0].model_dump_json()
    report = gap_rules.build_report(store, NOW)
    dumped = report.model_dump_json()
    assert store.all()[0].model_dump_json() == before
    assert "김하늘" not in dumped and "질문입니다" not in dumped
    assert any("시드" in n for n in report.notes)
    assert item(report, "refund").owner_target == "kb_owner"  # 부서 이름이 아닌 코드


def call(client: TestClient, skill: str, input: dict[str, Any], key: str | None = None) -> Any:
    body = {
        "meta": {
            "context_id": "ctx_inq_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input,
        "untrusted": {},
    }
    return client.post("/a2a", json=body).json()


def test_skill_is_registered_as_read_and_invalid_input_fails() -> None:
    card = load_card()
    skill = next(s for s in card.skills if s.id == "knowledge_gap_report")
    assert skill.grade == "read" and skill.id in ALL_HANDLERS
    client = TestClient(app)
    assert call(client, "knowledge_gap_report", {"window_days": 0})["status"] == "failed"
    ok = call(client, "knowledge_gap_report", {})
    assert ok["status"] == "completed" and ok["output"]["window_days"] == 7


def test_draft_decisions_now_record_category_sources_reason_and_blocked_sends() -> None:
    client = TestClient(app)

    def create() -> int:
        body = {
            "meta": {
                "context_id": "ctx_inq_0001",
                "token": "mock:dashboard-api",
                "path": [],
                "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                "idempotency_key": "k-gap-create",
            },
            "skill": "create_inquiry",
            "input": {"user_id": 1, "customer_name": "김하늘", "order_id": 101, "order_no": "O-1"},
            "untrusted": {"subject": "환불", "message": "표지가 찢어져 왔어요. 환불해 주세요."},
        }
        return client.post("/a2a", json=body).json()["output"]["id"]

    inquiry_id = create()

    def send(text: str, key: str, input: dict[str, Any]) -> Any:
        body = {
            "meta": {
                "context_id": "ctx_inq_0001",
                "token": "mock:dashboard-api",
                "path": [],
                "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
                "idempotency_key": key,
            },
            "skill": "send_reply",
            "input": {"inquiry_id": inquiry_id, **input},
            "untrusted": {"text": text},
        }
        return client.post("/a2a", json=body).json()

    blocked = send("전액 환불해 드리겠습니다", "k-gap-b", {"draft_style": "concise"})
    assert blocked["outcome"] == "blocked_by_guard"
    last = handlers.STORE.draft_events[-1]
    assert last["result"] == "blocked" and "promise_refund" in last["guard_codes"]
    assert "환불해 드리겠습니다" not in str(last)  # 답변 원문은 남기지 않는다

    send("확인 후 안내드리겠습니다", "k-gap-a", {"draft_style": "concise"})
    accepted = handlers.STORE.draft_events[-1]
    assert accepted["result"] == "accepted" and accepted["category"]
    assert accepted["message_id"] and accepted["occurred_at"]

    call(
        client,
        "reject_draft",
        {"inquiry_id": inquiry_id, "draft_style": "empathetic", "reason_code": "missing_policy"},
        key="k-gap-r",
    )
    assert handlers.STORE.draft_events[-1]["reason_code"] == "missing_policy"
    # reason_code 없이도 기존대로 동작한다
    call(
        client, "reject_draft", {"inquiry_id": inquiry_id, "draft_style": "concise"}, key="k-gap-r2"
    )
    assert handlers.STORE.draft_events[-1]["reason_code"] is None
