import pytest
from fastapi.testclient import TestClient

from ax_dashboard_api.main import app

BASE = "/api/dashboard"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/users",
        "/metrics",
        "/kpis",
        "/kb-chunks",
        "/conversations",
        "/conversations/1",
        "/orders?user_id=1",
        "/claims",
        "/claims/stats",
        "/claims/1",
        "/queue-entries",
        "/reviews",
    ],
)
def test_get_returns_data_meta_envelope(client: TestClient, path: str) -> None:
    response = client.get(BASE + path)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "meta"}
    assert body["meta"]["tenant_id"] == "shop-demo"


def test_list_meta_total_matches_data(client: TestClient) -> None:
    body = client.get(f"{BASE}/conversations").json()
    assert body["meta"]["total"] == len(body["data"]) == 2


def test_conversation_detail_has_drafts_with_guard(client: TestClient) -> None:
    data = client.get(f"{BASE}/conversations/1").json()["data"]
    drafts = data["messages"][0]["analysis"]["drafts"]
    assert {d["style"] for d in drafts} == {"empathetic", "concise", "step_by_step"}
    assert any(not d["validation"]["passed"] for d in drafts)


def test_claims_filter_by_status(client: TestClient) -> None:
    data = client.get(f"{BASE}/claims", params={"status": "needs_review"}).json()["data"]
    assert [c["id"] for c in data] == [1]


def test_claim_stats_counted_from_data(client: TestClient) -> None:
    stats = client.get(f"{BASE}/claims/stats").json()["data"]
    claims = client.get(f"{BASE}/claims").json()["data"]
    assert stats["total"] == len(claims)
    assert sum(stats["by_status"].values()) == len(claims)


def test_claim_detail_has_audit_and_receipt(client: TestClient) -> None:
    data = client.get(f"{BASE}/claims/1").json()["data"]
    assert data["audit"] and data["receipt"]["order_no"] == "ORD-2026-0101"


def test_metrics_totals_match_lists(client: TestClient) -> None:
    data = client.get(f"{BASE}/metrics", params={"days": 7}).json()["data"]
    assert data["days"] == 7
    assert data["totals"]["inquiries"] == 2
    assert data["totals"]["queue_waiting"] == len(data["queue"])


def test_not_found_uses_error_envelope(client: TestClient) -> None:
    response = client.get(f"{BASE}/claims/999")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "클레임 999를 찾을 수 없다"}
    }


def test_invalid_query_uses_error_envelope(client: TestClient) -> None:
    response = client.get(f"{BASE}/metrics", params={"days": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_without_mock_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    response = TestClient(app).get(f"{BASE}/claims")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "UPSTREAM_NOT_CONNECTED"
