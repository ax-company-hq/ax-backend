from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_agent import handlers, insights
from support_agent.insights import detect_surge, overview
from support_agent.main import ALL_HANDLERS, app, load_card
from support_agent.schemas import Analysis, Classification, Draft, Finding, Validation
from support_agent.store import InquiryStore

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def analysis(category: str, urgency: str = "medium", blocked: bool = False, ms=(100, 200)):
    findings = [Finding(code="promise_refund", level="error", message="x")] if blocked else []
    passed = not blocked
    return Analysis(
        classification=Classification(
            intent_ko="문의",
            category=category,
            sentiment="calm",
            urgency=urgency,  # type: ignore[arg-type]
            search_query=category,
        ),
        drafts=[
            Draft(
                style="concise",
                reply="답변",
                validation=Validation(passed=passed, findings=findings),
            )
        ],
        timings_ms={"classify": ms[0], "draft": ms[1]},
    )


def add(store: InquiryStore, category: str, minutes_ago: float, **kw: Any) -> int:
    conversation = store.create(
        context_id="ctx_req_0001",
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="문의",
        order_id=None,
        order_no=None,
        first_message="본문",
        analysis=analysis(category, **kw),
    )
    conversation.messages[0].created_at = iso(minutes_ago)
    conversation.updated_at = iso(minutes_ago)
    return conversation.id


# ---------------------------------------------------------------- 급증
def test_surge_detected_when_recent_count_far_above_baseline() -> None:
    store = InquiryStore()
    for _ in range(6):
        add(store, "shipping", minutes_ago=10)  # 최근 1시간 6건
    for hours in range(2, 26, 4):  # 평소: 24시간 동안 6건 → 시간당 0.25건
        add(store, "shipping", minutes_ago=hours * 60)
    report = detect_surge(store, NOW)
    assert [s.topic_code for s in report.surges] == ["shipping"]
    surge = report.surges[0]
    assert surge.count == 6
    assert surge.baseline_per_window == 0.25
    assert surge.ratio == 24.0
    assert len(surge.sample_inquiry_ids) == 3  # 샘플은 ID만, 최대 3개


def test_below_min_count_or_ratio_is_not_a_surge() -> None:
    store = InquiryStore()
    for _ in range(4):
        add(store, "shipping", minutes_ago=5)  # min_count 5 미만
    for _ in range(6):
        add(store, "refund", minutes_ago=5)  # 평소도 많다
    for hours in range(2, 26):
        add(store, "refund", minutes_ago=hours * 60)  # 평소 시간당 1건 → 6배지만 아래에서 확인
    report = detect_surge(store, NOW, ratio=10.0)
    assert report.surges == []
    assert report.total_in_window == 10
    # 같은 데이터에서 기준만 낮추면(평소의 3배) refund 6건은 급증이다
    assert [s.topic_code for s in detect_surge(store, NOW, ratio=3.0).surges] == ["refund"]


def test_zero_baseline_uses_count_only_and_ratio_is_none() -> None:
    store = InquiryStore()
    for _ in range(5):
        add(store, "payment", minutes_ago=3)
    surge = detect_surge(store, NOW).surges[0]
    assert surge.ratio is None and surge.baseline_per_window == 0


def test_messages_outside_windows_are_ignored() -> None:
    store = InquiryStore()
    for _ in range(8):
        add(store, "shipping", minutes_ago=60 * 30)  # 기준 기간보다 오래됨
    assert detect_surge(store, NOW).total_in_window == 0


def test_surge_report_has_no_message_text() -> None:
    store = InquiryStore()
    for _ in range(5):
        add(store, "payment", minutes_ago=3)
    assert "본문" not in detect_surge(store, NOW).model_dump_json()


# ---------------------------------------------------------------- KPI
def test_kpis_counts_come_from_records() -> None:
    store = InquiryStore()
    add(store, "refund", 5, urgency="high", blocked=True, ms=(100, 300))
    add(store, "shipping", 5, ms=(200, 400))
    store.draft_events += [
        {"inquiry_id": 1, "style": "concise", "result": "accepted", "edit_ratio": None},
        {"inquiry_id": 2, "style": "concise", "result": "edited", "edit_ratio": 0.4},
        {"inquiry_id": 2, "style": "concise", "result": "rejected"},
    ]
    kpis = insights.kpis(store)
    assert kpis.conversations == 2
    assert kpis.by_category == {"refund": 1, "shipping": 1}
    assert kpis.by_urgency == {"high": 1, "medium": 1}
    assert kpis.drafts_generated == 2 and kpis.drafts_blocked_by_guard == 1
    assert kpis.guard_findings == {"promise_refund": 1}
    assert (kpis.accepted_as_is, kpis.edited, kpis.rejected) == (1, 1, 1)
    assert kpis.draft_adoption_rate == 0.667  # (1 + 1) / 3
    assert kpis.avg_edit_ratio == 0.4
    assert kpis.avg_first_draft_ms == 500  # (400 + 600) / 2


def test_kpi_rates_are_none_without_data() -> None:
    kpis = insights.kpis(InquiryStore())
    assert kpis.draft_adoption_rate is None
    assert kpis.avg_edit_ratio is None and kpis.avg_first_draft_ms is None
    assert kpis.conversations == 0


def test_waiting_list_orders_by_urgency_then_wait() -> None:
    store = InquiryStore()
    low_old = add(store, "etc", 300, urgency="low")
    high_new = add(store, "refund", 5, urgency="high")
    high_old = add(store, "refund", 120, urgency="high")
    answered = add(store, "etc", 10)
    store.add_agent_message(answered, "답변")
    waiting = overview(store, NOW).waiting
    assert [w.id for w in waiting] == [high_old, high_new, low_old]
    assert waiting[0].waiting_minutes == 120


def test_resolved_conversations_are_not_waiting() -> None:
    store = InquiryStore()
    inquiry = add(store, "etc", 30)
    store.set_status(inquiry, "resolved")
    assert overview(store, NOW).waiting == []


# ---------------------------------------------------------------- skill
@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    return TestClient(app)


def call(client: TestClient, skill: str, input: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_req_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": skill,
        "input": input or {},
    }
    return client.post("/a2a", json=body).json()


def test_skills_are_declared_and_read_only() -> None:
    card = {s.id: s for s in load_card().skills}
    for skill in ("inquiry_overview", "detect_inquiry_surge"):
        assert skill in ALL_HANDLERS
        assert card[skill].grade == "read"


def test_overview_skill_returns_kpis_and_waiting(client: TestClient) -> None:
    result = call(client, "inquiry_overview")
    assert result["status"] == "completed"
    assert set(result["output"]) == {"kpis", "waiting"}


def test_surge_skill_validates_input(client: TestClient) -> None:
    assert call(client, "detect_inquiry_surge", {"ratio": 0.5})["status"] == "failed"
    ok = call(client, "detect_inquiry_surge", {"window_minutes": 30})
    assert ok["output"]["window_minutes"] == 30 and ok["output"]["surges"] == []


def test_send_reply_records_edit_ratio(client: TestClient) -> None:
    created = call(
        client,
        "create_inquiry",
        {"user_id": 1, "customer_name": "김하늘"},
    )
    assert created["status"] == "failed"  # 본문 없이는 접수되지 않는다 (untrusted 필수)
    body: dict[str, Any] = {
        "meta": {
            "context_id": "ctx_req_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": "k-c",
        },
        "skill": "create_inquiry",
        "input": {"user_id": 1, "customer_name": "김하늘"},
        "untrusted": {"subject": "제목", "message": "배송이 늦어요"},
    }
    inquiry_id = client.post("/a2a", json=body).json()["output"]["id"]
    body["meta"]["idempotency_key"] = "k-r"
    body["skill"] = "send_reply"
    body["input"] = {
        "inquiry_id": inquiry_id,
        "draft_style": "concise",
        "draft_text": "안녕하세요 확인 중입니다",
    }
    body["untrusted"] = {"text": "안녕하세요 확인 후 바로 안내드리겠습니다"}
    client.post("/a2a", json=body)
    event = handlers.STORE.draft_events[0]
    assert event["result"] == "edited"
    assert 0 < event["edit_ratio"] < 1
