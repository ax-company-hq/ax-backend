"""관리자 화면용 총괄 읽기 중계."""

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_shop_api import orchestrator_relay
from ax_shop_api.db import Db
from ax_shop_api.main import create_app

ADMIN = {"Authorization": "Bearer mock:admin"}
INFRA = {"Authorization": "Bearer mock:infra"}
INCIDENT = {"context_id": "ctx_inc_abc123", "status": "open", "steps": [], "shared": {}}


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SHOP_MONITOR_INTERVAL_S", "0")
    events.reset_mock_bus()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Db(":memory:")))


def _upstream(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(orchestrator_relay, "TRANSPORT", httpx.MockTransport(wrapped))
    return seen


def test_lists_incidents_from_orchestrator(client, monkeypatch) -> None:
    seen = _upstream(
        monkeypatch,
        lambda r: httpx.Response(200, json={"data": [INCIDENT], "meta": {"mock": True}}),
    )
    response = client.get("/api/shop/admin/orchestrator/incidents", headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"data": [INCIDENT], "meta": {"mock": True}}
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/api/incidents")]


def test_gets_one_incident(client, monkeypatch) -> None:
    seen = _upstream(
        monkeypatch, lambda r: httpx.Response(200, json={"data": INCIDENT, "meta": {}})
    )
    response = client.get("/api/shop/admin/orchestrator/incidents/ctx_inc_abc123", headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["data"]["context_id"] == "ctx_inc_abc123"
    assert seen[0].url.path == "/api/incidents/ctx_inc_abc123"


def test_unknown_incident_is_404(client, monkeypatch) -> None:
    _upstream(monkeypatch, lambda r: httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}))
    response = client.get("/api/shop/admin/orchestrator/incidents/ctx_inc_00", headers=ADMIN)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("context_id", ["..%2Fhealth", "ctx_inc_XYZ", "abc", "ctx_inc_"])
def test_bad_context_id_never_reaches_orchestrator(client, monkeypatch, context_id) -> None:
    seen = _upstream(monkeypatch, lambda r: httpx.Response(200, json={"data": {}, "meta": {}}))
    response = client.get(f"/api/shop/admin/orchestrator/incidents/{context_id}", headers=ADMIN)
    assert response.status_code in (400, 404)
    assert seen == []


def test_only_admin_may_read(client, monkeypatch) -> None:
    seen = _upstream(monkeypatch, lambda r: httpx.Response(200, json={"data": [], "meta": {}}))
    url = "/api/shop/admin/orchestrator/incidents"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=INFRA).status_code == 403
    assert seen == []


def test_orchestrator_down_is_502_not_a_crash(client, monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _upstream(monkeypatch, refuse)
    response = client.get("/api/shop/admin/orchestrator/incidents", headers=ADMIN)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert client.get("/health").status_code == 200  # shop-api는 그대로 산다


def test_timeout_is_504(client, monkeypatch) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    _upstream(monkeypatch, slow)
    response = client.get("/api/shop/admin/orchestrator/incidents", headers=ADMIN)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_malformed_upstream_body_is_502(client, monkeypatch) -> None:
    _upstream(monkeypatch, lambda r: httpx.Response(200, json=["not", "an", "envelope"]))
    response = client.get("/api/shop/admin/orchestrator/incidents", headers=ADMIN)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_relay_is_read_only(client) -> None:
    url = "/api/shop/admin/orchestrator/incidents"
    for method in ("post", "patch", "delete", "put"):
        assert getattr(client, method)(url, headers=ADMIN).status_code == 405


def test_orchestrator_url_comes_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("SHOP_ORCHESTRATOR_URL", "http://orchestrator:8000/")
    assert orchestrator_relay.base_url() == "http://orchestrator:8000"
    monkeypatch.delenv("SHOP_ORCHESTRATOR_URL")
    assert orchestrator_relay.base_url() == orchestrator_relay.DEFAULT_URL


DEPARTMENTS = [
    {
        "name": "support",
        "owner": "병환",
        "mode": "mock",
        "available": True,
        "skills": [],
        "usage": {"calls": 3},
    }
]
DEPARTMENTS_URL = "/api/shop/admin/orchestrator/departments"


def test_departments_are_passed_through_with_window_meta(client, monkeypatch) -> None:
    meta = {"mock": True, "window_seconds": 3600, "bucket_seconds": 300}
    seen = _upstream(
        monkeypatch, lambda r: httpx.Response(200, json={"data": DEPARTMENTS, "meta": meta})
    )
    response = client.get(DEPARTMENTS_URL, headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"data": DEPARTMENTS, "meta": meta}
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/api/departments")]


def test_departments_meta_keeps_only_known_numbers(client, monkeypatch) -> None:
    meta = {"mock": True, "window_seconds": "soon", "bucket_seconds": True, "extra": 1}
    _upstream(monkeypatch, lambda r: httpx.Response(200, json={"data": [], "meta": meta}))
    assert client.get(DEPARTMENTS_URL, headers=ADMIN).json()["meta"] == {"mock": True}


def test_departments_only_admin_may_read(client, monkeypatch) -> None:
    seen = _upstream(monkeypatch, lambda r: httpx.Response(200, json={"data": [], "meta": {}}))
    assert client.get(DEPARTMENTS_URL).status_code == 401
    assert client.get(DEPARTMENTS_URL, headers=INFRA).status_code == 403
    assert seen == []


def test_departments_orchestrator_down_is_502(client, monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _upstream(monkeypatch, refuse)
    response = client.get(DEPARTMENTS_URL, headers=ADMIN)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"


def test_departments_real_mode_501_is_upstream_error(client, monkeypatch) -> None:
    _upstream(monkeypatch, lambda r: httpx.Response(501, json={"detail": "not in real mode"}))
    response = client.get(DEPARTMENTS_URL, headers=ADMIN)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_departments_relay_is_read_only(client) -> None:
    for method in ("post", "patch", "delete", "put"):
        assert getattr(client, method)(DEPARTMENTS_URL, headers=ADMIN).status_code == 405
