"""화면용 REST·SSE (`/api/planning/*`)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from planning_agent.main import app

BASE = "/api/planning"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_get_state_envelope(client) -> None:
    body = client.get(f"{BASE}/state").json()
    assert set(body) == {"data", "meta"}
    assert body["meta"]["tenant_id"] == "shop-demo"
    data = body["data"]
    assert len(data["requests"]) == 6
    assert data["requests"][0]["from"] == "data"  # 프로토타입과 같은 `from` 키
    assert data["requests"][0]["context_id"] == "ctx_req_0009"
    assert data["prs"][0]["merged_at"] == "2026-10-21T05:02:00Z"
    assert data["auto_policy"]["level"] == 1


@pytest.mark.parametrize(
    ("path", "count"),
    [
        ("requests", 6),
        ("requests?state=접수", 3),
        ("specs", 2),
        ("tasks", 1),
        ("pull-requests", 1),
        ("threads", 8),
        ("decisions", 1),
        ("incident-links", 1),
        ("docs", 1),
        ("projects", 5),
    ],
)
def test_list_endpoints(client, path, count) -> None:
    body = client.get(f"{BASE}/{path}").json()
    assert len(body["data"]) == count
    assert body["meta"]["count"] == count


def test_detail_endpoints(client) -> None:
    metrics = client.get(f"{BASE}/metrics").json()["data"]
    assert metrics["first_pr_minutes_avg"] == 38
    assert metrics["draft_adoption_rate"] == 0
    project = client.get(f"{BASE}/projects/REQ-9").json()["data"]
    assert [s["done"] for s in project["stages"]] == [True, True, True, True, True, False]
    assert project["spec"]["id"] == "SPEC-5"
    for pr_ref in ("28", "PR-28", "PR%20%2328"):
        trace = client.get(f"{BASE}/trace/{pr_ref}").json()["data"]
        assert trace["pr"] == "PR #28" and trace["steps"][0]["kind"] == "코드"
    policy = client.get(f"{BASE}/auto-policy").json()["data"]
    assert policy["limit"] == {"level": 1, "lines": 30, "files": 2, "paths": "shop-web/"}
    assert policy["promote_after"] == 3 and len(policy["levels"]) == 4


def test_projects_sorted_in_progress_first(client) -> None:
    ids = [p["id"] for p in client.get(f"{BASE}/projects").json()["data"]]
    assert ids == ["REQ-12", "REQ-13", "REQ-14", "REQ-101", "REQ-9"]


def test_post_action_prototype_shape(client) -> None:
    res = client.post(f"{BASE}/actions", json={"type": "confirmSpec", "spec": "SPEC-7"})
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["changed"] is True
    spec = next(s for s in body["data"]["specs"] if s["id"] == "SPEC-7")
    assert spec["status"] == "확정"
    # snake_case 이름도 받는다
    res = client.post(f"{BASE}/actions", json={"type": "inbound", "kind": "support_simple"})
    assert res.json()["meta"]["result"] == {"request_id": "REQ-21", "auto": True}
    res = client.post(f"{BASE}/actions", json={"type": "deptChat", "from": "hr"})
    assert res.json()["meta"]["changed"] is True
    state = client.get(f"{BASE}/state").json()["data"]
    assert state["specs"][0]["status"] == "확정"


def test_reset(client) -> None:
    client.post(f"{BASE}/actions", json={"type": "inbound", "kind": "hr_draft"})
    body = client.post(f"{BASE}/reset").json()
    assert len(body["data"]["requests"]) == 6


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"type": "nope"}, 400, "UNKNOWN_ACTION"),
        ({"spec": "SPEC-7"}, 400, "INVALID_ACTION"),
        ({"type": "confirmSpec"}, 400, "INVALID_ACTION"),
        ({"type": "inbound", "kind": "unknown_kind"}, 400, "INVALID_ACTION"),
    ],
)
def test_action_error_envelope(client, payload, status, code) -> None:
    res = client.post(f"{BASE}/actions", json=payload)
    assert res.status_code == status
    assert set(res.json()) == {"error"}
    assert res.json()["error"]["code"] == code
    assert res.json()["error"]["message"]


def test_not_found_and_bad_json_envelopes(client) -> None:
    res = client.get(f"{BASE}/projects/REQ-999")
    assert (res.status_code, res.json()["error"]["code"]) == (404, "NOT_FOUND")
    res = client.get(f"{BASE}/trace/999")
    assert (res.status_code, res.json()["error"]["code"]) == (404, "NOT_FOUND")
    res = client.post(
        f"{BASE}/actions", content=b"not json", headers={"content-type": "application/json"}
    )
    assert (res.status_code, res.json()["error"]["code"]) == (400, "INVALID_ACTION")


def test_simulator_actions_need_mock_mode(client, monkeypatch) -> None:
    for key in ("AGENT_TEMPLATE_MOCK", "PLANNING_MOCK"):
        monkeypatch.delenv(key, raising=False)
    res = client.post(f"{BASE}/actions", json={"type": "inbound", "kind": "support_ticket"})
    assert (res.status_code, res.json()["error"]["code"]) == (403, "SIMULATOR_DISABLED")


def _events(text: str) -> list[dict]:
    out = []
    for chunk in text.split("\n\n"):
        lines = chunk.splitlines()
        if "event: state" in lines:
            data = next(line for line in lines if line.startswith("data: "))
            out.append(json.loads(data.removeprefix("data: ")))
    return out


def test_sse_sends_state_on_change(service) -> None:
    with TestClient(app) as client:
        with client.stream("GET", f"{BASE}/events?max_events=2") as res:
            assert res.headers["content-type"].startswith("text/event-stream")
            lines = res.iter_lines()
            first: list[str] = []
            for line in lines:
                if line == "" and first:
                    break
                first.append(line)
            assert "event: state" in first
            assert service.subscriber_count == 1
            client.post(f"{BASE}/actions", json={"type": "inbound", "kind": "hr_draft"})
            rest = "\n".join(lines)
    events = _events("\n".join(first) + "\n\n" + rest)
    assert len(events) == 2
    assert len(events[0]["requests"]) == 6
    assert len(events[1]["requests"]) == 7  # 상태 변경 뒤 새 상태
    assert service.subscriber_count == 0
