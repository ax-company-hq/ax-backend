import json

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_contracts.schemas import EventType, ShopDegradedData
from ax_shop_api.db import Db
from ax_shop_api.main import create_app
from ax_shop_api.monitor import Monitor, Thresholds

ADMIN = {"Authorization": "Bearer mock:admin"}
INFRA = {"Authorization": "Bearer mock:infra"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SHOP_MONITOR_INTERVAL_S", "0")  # 주기 판정은 끄고 직접 평가한다
    events.reset_mock_bus()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(clock: Clock) -> TestClient:
    app = create_app(Db(":memory:"))
    app.state.shop.clock = clock
    return TestClient(app)


def _pay(client: TestClient, simulate: str | None = None) -> dict:
    body = {"buyer_id": "buy_0001", "items": [{"product_id": "prd_0001", "qty": 1}]}
    order = client.post("/api/shop/orders", json=body).json()["data"]
    paid = client.post(
        f"/api/shop/orders/{order['order_id']}/payments", json={"simulate": simulate}
    )
    return paid.json()["data"]


def _types() -> list[str]:
    return [e.type.value for e in events.MOCK_PUBLISHED]


# --- 관리 API 권한 ----------------------------------------------------------


def test_admin_api_requires_a_valid_token_and_allowed_requester(client: TestClient) -> None:
    body = {"kind": "failure"}
    assert client.post("/api/shop/admin/faults", json=body).status_code == 401
    bad = client.post("/api/shop/admin/faults", json=body, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    support = {"Authorization": "Bearer mock:support"}
    assert client.post("/api/shop/admin/faults", json=body, headers=support).status_code == 403
    assert client.post("/api/shop/admin/faults", json=body, headers=INFRA).status_code == 403
    assert client.post("/api/shop/admin/faults", json=body, headers=ADMIN).status_code == 201


def test_only_admin_or_infra_can_release(client: TestClient) -> None:
    client.post("/api/shop/admin/faults", json={"kind": "failure"}, headers=ADMIN)
    marketing = {"Authorization": "Bearer mock:marketing"}
    assert client.delete("/api/shop/admin/faults", headers=marketing).status_code == 403
    released = client.delete("/api/shop/admin/faults", headers=INFRA).json()["data"]
    assert released["active"] is False


# --- 장애 주입 효과 ---------------------------------------------------------


def test_failure_fault_makes_payments_fail_with_5xx_code(client: TestClient) -> None:
    client.post("/api/shop/admin/faults", json={"kind": "failure"}, headers=ADMIN)
    order = _pay(client)
    assert order["status"] == "payment_failed"
    assert order["payments"][0]["failure_code"] == "PG_UNAVAILABLE"
    client.delete("/api/shop/admin/faults", headers=ADMIN)
    assert _pay(client)["status"] == "paid"


def test_card_decline_is_not_counted_as_5xx(clock: Clock) -> None:
    monitor = Monitor(Db(":memory:"), Thresholds(sustain_s=0, min_samples=3))
    for _ in range(5):
        monitor.record(clock(), 180.0, server_error=False)  # 카드 거절은 5xx가 아니다
    monitor.evaluate(clock())
    assert not monitor.degraded


# --- shop.degraded / shop.recovered 판정 -------------------------------------


def _monitor() -> tuple[Monitor, Db]:
    db = Db(":memory:")
    return Monitor(db, Thresholds(p95_latency_ms=1000, error_rate_5xx=0.02, min_samples=5)), db


def _outbox(db: Db) -> list[dict]:
    with db.tx() as conn:
        rows = conn.execute("SELECT payload FROM outbox_events ORDER BY rowid").fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _outbox_types(db: Db) -> list[str]:
    return [event["type"] for event in _outbox(db)]


def test_degraded_only_after_the_breach_lasts_one_minute() -> None:
    monitor, db = _monitor()
    for second in range(0, 61, 5):
        monitor.record(float(second), 1500.0, False)
        monitor.evaluate(float(second))
        if second < 60:
            assert not monitor.degraded and not _outbox_types(db)
    assert monitor.degraded
    assert _outbox_types(db) == ["shop.degraded"]
    monitor.record(65.0, 1500.0, False)
    monitor.evaluate(65.0)  # 이미 장애 상태면 다시 발행하지 않는다
    assert _outbox_types(db) == ["shop.degraded"]


def test_too_few_samples_do_not_trigger() -> None:
    monitor, db = _monitor()
    for second in (0.0, 30.0, 90.0):
        monitor.record(second, 5000.0, True)
        monitor.evaluate(second)
    assert not monitor.degraded and not _outbox_types(db)


def test_error_rate_breach_reports_the_5xx_metric() -> None:
    monitor, db = _monitor()
    for second in range(0, 61, 5):
        monitor.record(float(second), 200.0, server_error=second % 10 == 0)
        monitor.evaluate(float(second))
    assert monitor.degraded
    data = ShopDegradedData.model_validate(_outbox(db)[0]["data"])
    assert (data.metric_code, data.threshold) == ("error_rate_5xx", 0.02)
    assert data.value is not None and data.value > 0.02


def test_recovery_once_traffic_is_normal_again() -> None:
    monitor, db = _monitor()
    for second in range(0, 61, 5):
        monitor.record(float(second), 1500.0, False)
        monitor.evaluate(float(second))
    for second in range(70, 140, 5):  # 정상 표본이 창을 채운다
        monitor.record(float(second), 180.0, False)
        monitor.evaluate(float(second))
    assert not monitor.degraded
    assert _outbox_types(db) == ["shop.degraded", "shop.recovered"]


def _trip_degraded(monitor: Monitor) -> None:
    for second in range(0, 61, 5):
        monitor.record(float(second), 1500.0, False)
        monitor.evaluate(float(second))


def test_event_ids_do_not_repeat_after_the_db_is_recreated() -> None:
    """DB를 새로 만들면 순번이 dgd_0001로 돌아가도 이벤트 ID는 지난 장애와 달라야 한다."""
    first, first_db = _monitor()
    second, second_db = _monitor()  # DB 초기화·재배포와 같은 상황
    _trip_degraded(first)
    _trip_degraded(second)
    first_event, second_event = _outbox(first_db)[0], _outbox(second_db)[0]
    assert first_event["type"] == second_event["type"] == "shop.degraded"
    assert first_event["event_id"] != second_event["event_id"]
    assert first_event["idempotency_key"] != second_event["idempotency_key"]
    assert first_event["idempotency_key"].startswith("shop.degraded:dgd_0001-")  # 순번 규칙은 유지
    assert second_event["idempotency_key"].startswith("shop.degraded:dgd_0001-")


def test_reset_publishes_recovered_only_if_degraded() -> None:
    monitor, db = _monitor()
    monitor.reset()
    assert _outbox_types(db) == []
    for second in range(0, 61, 5):
        monitor.record(float(second), 1500.0, False)
        monitor.evaluate(float(second))
    monitor.reset()
    monitor.reset()  # 두 번 해제해도 한 번만
    assert _outbox_types(db) == ["shop.degraded", "shop.recovered"]


# --- 시나리오 A 흐름: 주입 → degraded → infra가 해제 → recovered ---------------


def test_inject_degrade_and_release_by_infra(client: TestClient, clock: Clock) -> None:
    client.post("/api/shop/admin/faults", json={"kind": "latency", "delay_ms": 2000}, headers=ADMIN)
    for _ in range(8):
        _pay(client)
        clock.now += 9  # 72초 동안 결제가 이어진다
    assert client.get("/api/shop/admin/faults", headers=ADMIN).json()["data"]["degraded"] is True
    degraded = [e for e in events.MOCK_PUBLISHED if e.type == EventType.SHOP_DEGRADED]
    assert len(degraded) == 1
    data = ShopDegradedData.model_validate(degraded[0].data)
    assert (data.component, data.metric_code, data.threshold) == ("payment", "p95_latency_ms", 1000)
    assert data.value == 2180.0

    client.delete("/api/shop/admin/faults", headers=INFRA)  # 롤백이 해제 API를 호출한다
    assert [t for t in _types() if t.startswith("shop.")] == ["shop.degraded", "shop.recovered"]
    recovered = events.MOCK_PUBLISHED[-1]
    assert ShopDegradedData.model_validate(recovered.data).value is None  # 새 측정값 없음
    assert degraded[0].idempotency_key.split(":")[1] == recovered.idempotency_key.split(":")[1]
    assert all(e.context_id is None for e in events.MOCK_PUBLISHED)  # 사건 번호는 통합 에이전트 몫
