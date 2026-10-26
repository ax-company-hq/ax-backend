"""문의 급증 감지의 중앙값 기준선: 지난 기간에 몰린 한 번이 평소를 부풀리지 않는다."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_agent import handlers
from support_agent.insight_schemas import SurgeInput
from support_agent.insights import detect_surge
from support_agent.main import app
from support_agent.schemas import Analysis, Classification
from support_agent.store import InquiryStore

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(store: InquiryStore, minutes_ago: float, category: str = "shipping") -> None:
    conversation = store.create(
        context_id="ctx_req_0001",
        tenant_id="shop-demo",
        user_id=1,
        customer_name="김하늘",
        subject="문의",
        order_id=None,
        order_no=None,
        first_message="본문",
        analysis=Analysis(
            classification=Classification(
                intent_ko="문의",
                category=category,
                sentiment="calm",
                urgency="medium",
                search_query=category,
            )
        ),
    )
    conversation.messages[0].created_at = iso(minutes_ago)


def spiky_store() -> InquiryStore:
    """최근 1시간에 6건. 기준 24시간은 2시간 전 한 시간에 몰린 60건뿐이고 나머지 23시간은 0건."""
    store = InquiryStore()
    for _ in range(6):
        add(store, 10)
    for minute in range(0, 60):
        add(store, 125 + minute % 55)  # 2시간 5분 ~ 3시간 전: 같은 창에 60건
    return store


def test_mean_is_inflated_by_one_busy_hour_so_the_surge_is_missed() -> None:
    report = detect_surge(spiky_store(), NOW)  # 기본은 평균
    assert report.baseline_method == "mean"
    assert report.surges == []  # 평소가 60/24=2.5건으로 부풀어 6건은 2.4배(기준 3배 미만)


def test_median_ignores_the_one_busy_hour_and_still_sees_the_surge() -> None:
    report = detect_surge(spiky_store(), NOW, baseline_method="median")
    assert report.baseline_method == "median"
    (surge,) = report.surges
    assert surge.topic_code == "shipping" and surge.count == 6
    assert surge.baseline_per_window == 0.0 and surge.ratio is None  # 평소(중앙값)는 0건


def test_median_counts_empty_windows_as_zero_not_as_missing() -> None:
    store = InquiryStore()
    for _ in range(6):
        add(store, 10)
    for hours in (2, 4, 6):  # 24개 창 중 3개 창에만 1건씩
        add(store, hours * 60 - 30)
    (surge,) = detect_surge(store, NOW, baseline_method="median").surges
    assert surge.baseline_per_window == 0.0


def test_median_of_a_steady_baseline_equals_the_steady_rate() -> None:
    store = InquiryStore()
    for _ in range(8):
        add(store, 10)
    for window in range(1, 25):  # 24개 창 모두 2건씩
        add(store, window * 60 + 20)
        add(store, window * 60 + 40)
    report = detect_surge(store, NOW, baseline_method="median")
    surge = report.surges[0]
    assert surge.baseline_per_window == 2.0 and surge.ratio == 4.0


def test_default_method_and_numbers_stay_as_before() -> None:
    store = InquiryStore()
    for _ in range(6):
        add(store, 10)
    for hours in range(2, 26, 4):
        add(store, hours * 60)
    surge = detect_surge(store, NOW).surges[0]
    assert surge.baseline_per_window == 0.25 and surge.ratio == 24.0


def test_median_works_when_the_baseline_is_not_a_whole_number_of_windows() -> None:
    store = InquiryStore()
    for _ in range(5):
        add(store, 10)
    report = detect_surge(
        store, NOW, window_minutes=50, baseline_hours=24, baseline_method="median"
    )
    assert report.surges[0].baseline_per_window == 0.0


def test_input_accepts_only_known_methods() -> None:
    assert SurgeInput().baseline_method == "mean"
    assert SurgeInput(baseline_method="median").baseline_method == "median"
    with pytest.raises(ValueError):
        SurgeInput(baseline_method="average")  # type: ignore[arg-type]


def test_skill_passes_the_method_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    body: dict[str, Any] = {
        "meta": {
            "context_id": "ctx_ui_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "detect_inquiry_surge",
        "input": {"baseline_method": "median"},
    }
    result = TestClient(app).post("/a2a", json=body).json()
    assert result["status"] == "completed"
    assert result["output"]["baseline_method"] == "median"
