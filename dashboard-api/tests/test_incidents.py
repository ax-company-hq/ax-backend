"""AI 대화방 읽기 중계: GET /api/dashboard/incidents, /incidents/{id}, /executions."""

from fastapi.testclient import TestClient

from ax_dashboard_api import incidents
from ax_dashboard_api.main import app

BASE = "/api/dashboard"


def _live(monkeypatch):
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)


def test_mock_incidents_and_detail(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    client = TestClient(app)
    body = client.get(f"{BASE}/incidents").json()
    assert body["meta"]["total"] == 1
    inc = body["data"][0]
    assert {"steps", "events", "pending_holds"} <= set(inc)
    one = client.get(f"{BASE}/incidents/{inc['context_id']}")
    assert one.json()["data"]["context_id"] == inc["context_id"]
    assert client.get(f"{BASE}/incidents/ctx_inc_none").status_code == 404


def test_mock_executions_have_no_params(monkeypatch):
    monkeypatch.setenv("DASHBOARD_MOCK", "1")
    client = TestClient(app)
    data = client.get(f"{BASE}/executions").json()["data"]
    assert len(data) == 2
    assert "params" not in data[0] and "request" not in data[0]
    assert data[0]["outcome"] == "held" and data[0]["computed_amount"] == 400000
    assert client.get(f"{BASE}/executions", params={"context_id": "x"}).json()["data"] == []


def test_live_incidents_relay(monkeypatch):
    _live(monkeypatch)
    seen: list[tuple] = []

    async def fake_get(target, path, params=None):
        seen.append((target, path))
        return [{"context_id": "ctx_inc_1", "steps": []}]

    monkeypatch.setattr(incidents, "_get", fake_get)
    res = TestClient(app).get(f"{BASE}/incidents")
    assert res.json()["data"] == [{"context_id": "ctx_inc_1", "steps": []}]
    assert seen == [("orchestrator", "/api/incidents")]


def test_live_executions_pick_safe_fields(monkeypatch):
    _live(monkeypatch)
    record = {
        "execution_id": "exe_1",
        "created_at": "2026-10-22T09:00:00Z",
        "request": {
            "action": "issue_coupon",
            "requester": "marketing",
            "context_id": "ctx_inc_1",
            "params": {"customer_email": "secret@example.com"},
        },
        "decision": {
            "execution_id": "exe_1",
            "grade": "B",
            "status": "verifying",
            "computed_amount": 20000,
        },
        "customer": "고객A",
        "report": {"calculation": "x"},
    }
    seen: list[dict | None] = []

    async def fake_get(target, path, params=None):
        seen.append(params)
        return [record]

    monkeypatch.setattr(incidents, "_get", fake_get)
    res = TestClient(app).get(f"{BASE}/executions", params={"context_id": "ctx_inc_1"})
    (line,) = res.json()["data"]
    assert line["outcome"] == "in_progress" and line["computed_amount"] == 20000
    assert "secret@example.com" not in res.text and "고객A" not in res.text
    assert seen == [{"context_id": "ctx_inc_1"}]


def test_live_upstream_errors(monkeypatch):
    _live(monkeypatch)

    async def down(target, path, params=None):
        from ax_dashboard_api.envelope import ApiError

        raise ApiError(502, "UPSTREAM_ERROR", "연결하지 못했다")

    monkeypatch.setattr(incidents, "_get", down)
    res = TestClient(app).get(f"{BASE}/executions")
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "UPSTREAM_ERROR"
