"""재무 대시보드 API 테스트 (재무 재설계 11장 응답 형식). 목 모드·가짜 안전 장치, 네트워크 없음."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_contracts.schemas import ExecuteDecision, HoldReason
from finance_agent import finance_api, handlers
from finance_agent.main import app
from finance_agent.sources import MockSources

HUMAN = {"Authorization": "Bearer human:taeyoon"}
META_KEYS = {"mock", "collected_at", "source", "unavailable"}


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch, fixed_now) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("FINANCE_HUMAN_TOKENS", raising=False)
    monkeypatch.delenv("SAFETY_HUMAN_TOKENS", raising=False)
    monkeypatch.delenv("AX_SAFETY_URL", raising=False)
    events.reset_mock_bus()
    handlers.reset_state(MockSources())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _get(client: TestClient, url: str, **params: Any) -> dict:
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"data", "meta"}
    assert set(body["meta"]) >= META_KEYS
    assert (body["meta"]["mock"], body["meta"]["source"]) == (True, "seed")
    datetime.fromisoformat(body["meta"]["collected_at"].replace("Z", "+00:00"))
    return body


def _fake_safety(monkeypatch: pytest.MonkeyPatch, decision: ExecuteDecision) -> list[dict]:
    calls: list[dict] = []

    async def fake(action: str, params: dict, context: Any, evidence: Any) -> ExecuteDecision:
        calls.append(
            {"action": action, "params": params, "key": context.idempotency_key, "ctx": context}
        )
        return decision

    monkeypatch.setattr(finance_api, "execute", fake)
    return calls


HELD = ExecuteDecision(
    execution_id="exe_77",
    grade="C",
    status="held",
    hold_reason=HoldReason.OVER_THRESHOLD,
    review_id="rev_77",
)


# ---------------------------------------------------------------- 조회 형식
def test_overview_shape(client: TestClient) -> None:
    data = _get(client, "/api/finance/overview")["data"]
    assert data["date"] == "2026-10-23"
    assert set(data["kpis"]) == {
        "gmv",
        "commission_revenue",
        "refund_total",
        "coupon_cost",
        "pg_fee",
        "net_result",
    }
    k = data["kpis"]
    assert k["net_result"] == k["commission_revenue"] - k["coupon_cost"] - k["pg_fee"]
    assert all(isinstance(v, int) for v in k.values())
    kinds = [t["kind"] for t in data["todo"]]
    assert kinds == [
        "settlement_batch",
        "closing_confirm",
        "recon_exception",
        "audit_finding",
        "policy_alert",
    ]
    for t in data["todo"]:
        assert set(t) == {"kind", "title", "count", "amount", "link"}
        assert t["count"] > 0
    batch = data["todo"][0]
    assert (batch["count"], batch["link"]) == (2, "settlements")
    assert batch["title"] == "판매자 정산 2곳 지급 승인 요청 대기"


def test_closing_shape_and_params(client: TestClient) -> None:
    day = _get(client, "/api/finance/closing")["data"]
    assert (day["period"], day["date"], day["status"]) == ("day", "2026-10-23", "draft")
    assert day["commentary_source"] == "template" and day["commentary"]
    assert [ln["code"] for ln in day["lines"]][-1] == "net_result"
    month = _get(client, "/api/finance/closing", period="month", date="2026-10-05")["data"]
    assert (month["date"], month["from"]) == ("2026-10-01", "2026-10-01T00:00:00Z")
    for bad in ({"period": "week"}, {"date": "어제"}):
        response = client.get("/api/finance/closing", params=bad)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_reconciliation_shape(client: TestClient) -> None:
    body = _get(client, "/api/finance/reconciliation")
    data = body["data"]
    assert set(data) == {"from", "to", "counts", "totals", "items"}
    assert set(data["counts"]) == {"matched", "mismatch", "missing_in_pg", "pending", "fee_anomaly"}
    assert set(data["totals"]) == {"order_amount", "pg_amount", "pg_fee", "pay_out"}
    assert body["meta"]["pg_source"] == "mock_toss_settlement"
    one = _get(client, "/api/finance/reconciliation", date="2026-10-20")["data"]
    assert (one["from"], one["to"]) == ("2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z")
    assert client.get("/api/finance/reconciliation", params={"date": "x"}).status_code == 400


def test_settlements_policy_audit_shapes(client: TestClient) -> None:
    sett = _get(client, "/api/finance/settlements")["data"]
    assert set(sett) == {"period", "from", "to", "pay_date", "statements", "batch"}
    assert sett["batch"]["status"] == "none"
    assert (
        _get(client, "/api/finance/settlements", period="2026-W41")["data"]["period"] == "2026-W41"
    )
    assert client.get("/api/finance/settlements", params={"period": "41"}).status_code == 400

    pol = _get(client, "/api/finance/policy")["data"]
    assert set(pol) == {"rules", "proposals", "how_to_change"}
    for p in pol["proposals"]:
        assert set(p) == {"key", "current", "proposed", "reason", "evidence"}

    aud = _get(client, "/api/finance/audit", days=7)["data"]
    assert set(aud) == {"from", "to", "stats", "findings"}
    assert _get(client, "/api/finance/audit", days=14)["data"]["from"] == "2026-10-10T00:00:00Z"
    for bad in ("0", "91", "x"):
        assert client.get("/api/finance/audit", params={"days": bad}).status_code == 400


def test_missing_sources_are_reported_in_meta(client: TestClient, kit) -> None:
    async def live_like():
        return kit.books(
            mock=False,
            source="shop-api",
            unavailable=["claims", "thresholds"],
            thresholds=None,
            claims=None,
        )

    handlers.state().books_loader = live_like
    body = client.get("/api/finance/policy").json()
    assert body["meta"]["unavailable"] == ["claims", "thresholds"]
    assert (body["meta"]["mock"], body["meta"]["source"]) == (False, "shop-api")
    sett = client.get("/api/finance/settlements").json()
    assert "hold_reasons.open_claim" in sett["meta"]["unavailable"]


# ---------------------------------------------------------------- 사람 결정
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer mock:finance"},
        {"Authorization": "Bearer nope"},
        {"Authorization": "human:taeyoon"},
    ],
)
def test_closing_confirm_needs_human_token(client: TestClient, headers: dict) -> None:
    response = client.post("/api/finance/closing/confirm", json={"period": "day"}, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert handlers.state().records.closings == {}


def test_closing_confirm_records_person(client: TestClient) -> None:
    body = {"period": "day", "date": "2026-10-22"}
    response = client.post("/api/finance/closing/confirm", json=body, headers=HUMAN)
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["status"], data["confirmed_by"], data["date"]) == (
        "confirmed",
        "taeyoon",
        "2026-10-22",
    )
    assert data["confirmed_at"].endswith("Z")
    again = client.get("/api/finance/closing", params={"date": "2026-10-22"}).json()["data"]
    assert (again["status"], again["confirmed_by"]) == ("confirmed", "taeyoon")
    kinds = [t["kind"] for t in client.get("/api/finance/overview").json()["data"]["todo"]]
    assert "closing_confirm" not in kinds  # 어제 결산을 확정했으니 할 일에서 빠진다
    other = client.post(
        "/api/finance/closing/confirm", json=body, headers={"Authorization": "Bearer human:someone"}
    )
    assert other.json()["data"]["confirmed_by"] == "taeyoon"  # 처음 확정한 사람이 남는다
    bad = client.post("/api/finance/closing/confirm", json={"period": "year"}, headers=HUMAN)
    assert bad.status_code == 400


def test_configured_human_tokens(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFETY_HUMAN_TOKENS", "byunghwan:s3cret")
    ok = client.post(
        "/api/finance/closing/confirm", json={}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.json()["data"]["confirmed_by"] == "byunghwan"
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK")  # 목 모드가 아니면 human:<이름>은 받지 않는다
    monkeypatch.delenv("FINANCE_MOCK", raising=False)  # CI는 FINANCE_MOCK=1도 켠다
    refused = client.post(
        "/api/finance/closing/confirm", json={"date": "2026-10-20"}, headers=HUMAN
    )
    assert refused.status_code == 401


def test_reconciliation_note(client: TestClient) -> None:
    items = client.get("/api/finance/reconciliation").json()["data"]["items"]
    target = next(i for i in items if i["status"] == "mismatch")
    url = f"/api/finance/reconciliation/{target['payment_id']}/note"
    assert client.post(url, json={"note": "PG사에 문의함"}).status_code == 401
    assert client.post(url, json={"note": " "}, headers=HUMAN).status_code == 400
    missing = client.post(
        "/api/finance/reconciliation/pay_none/note", json={"note": "x"}, headers=HUMAN
    )
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")
    response = client.post(url, json={"note": "PG사에 문의함"}, headers=HUMAN)
    assert response.status_code == 200
    item = response.json()["data"]
    assert (item["payment_id"], item["note"], item["status"]) == (
        target["payment_id"],
        "PG사에 문의함",
        "mismatch",
    )
    listed = client.get("/api/finance/reconciliation").json()["data"]["items"]
    assert (
        next(i for i in listed if i["payment_id"] == target["payment_id"])["note"]
        == "PG사에 문의함"
    )
    assert handlers.state().records.recon_note_log[0]["by"] == "taeyoon"


# ---------------------------------------------------------------- 정산 묶음 (execute → safety C)
def test_batch_goes_through_execute_and_is_held(client: TestClient, monkeypatch) -> None:
    calls = _fake_safety(monkeypatch, HELD)
    response = client.post("/api/finance/settlements/batch", json={"period": "2026-W42"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["batch"]["status"] == "requested"
    assert (data["batch"]["review_id"], data["batch"]["execution_id"]) == ("rev_77", "exe_77")
    assert data["batch"]["seller_ids"] == ["sel_0001", "sel_0003"]
    rows = {r["seller_id"]: r for r in data["statements"]}
    assert (rows["sel_0001"]["status"], rows["sel_0001"]["review_id"]) == ("requested", "rev_77")
    assert rows["sel_0002"]["status"] == "held"  # 보류 판매자는 묶음에서 빠진다
    total = rows["sel_0001"]["payout"] + rows["sel_0003"]["payout"]
    assert data["batch"]["total_payout"] == total

    [call] = calls
    assert call["action"] == "confirm_settlement"
    assert call["key"] == "ctx_req_settlement_2026-W42:confirm_settlement:2026-W42:1"
    assert call["ctx"].context_id == "ctx_req_settlement_2026-W42"
    params = call["params"]
    assert (params["period"], params["seller_ids"], params["total_payout"]) == (
        "2026-W42",
        ["sel_0001", "sel_0003"],
        total,
    )
    assert [s["payout"] for s in params["statements"]] == [
        rows["sel_0001"]["payout"],
        rows["sel_0003"]["payout"],
    ]
    assert "지급 승인 요청" in params["summary"] and params["pay_date"] == "2026-10-24"

    again = client.post("/api/finance/settlements/batch", json={"period": "2026-W42"})
    assert again.status_code == 200 and len(calls) == 1  # 같은 기간은 다시 요청하지 않는다
    todo = client.get("/api/finance/overview").json()["data"]["todo"][0]
    assert (todo["kind"], todo["amount"]) == ("settlement_batch", total)
    assert "안전 장치" in todo["title"]


def test_batch_returns_503_when_safety_is_down(client: TestClient, monkeypatch) -> None:
    down = ExecuteDecision(execution_id="exe_unavailable", grade="C", status="rejected")
    calls = _fake_safety(monkeypatch, down)
    response = client.post("/api/finance/settlements/batch", json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "safety_unavailable"
    assert len(calls) == 1 and handlers.state().records.batches == {}  # 지급 기록 없음
    after = client.get("/api/finance/settlements").json()["data"]
    assert after["batch"]["status"] == "none"


def test_batch_fails_closed_through_real_execute(client: TestClient, monkeypatch) -> None:
    """agent-template execute()가 safety에 못 닿으면(예외) rejected → 재무는 503."""
    monkeypatch.setenv("AX_SAFETY_URL", "http://safety.invalid")

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("down")

    from ax_agent_template import execute as template_execute

    monkeypatch.setattr(template_execute, "_send_to_safety", boom)
    response = client.post("/api/finance/settlements/batch", json={})
    assert (response.status_code, response.json()["error"]["code"]) == (503, "safety_unavailable")


def test_batch_token_optional_but_must_be_human(client: TestClient, monkeypatch) -> None:
    _fake_safety(monkeypatch, HELD)
    bad = client.post(
        "/api/finance/settlements/batch", json={}, headers={"Authorization": "Bearer mock:finance"}
    )
    assert bad.status_code == 401
    ok = client.post("/api/finance/settlements/batch", json={}, headers=HUMAN)
    assert ok.status_code == 200
    assert handlers.state().records.batches["2026-W42"]["requested_by"] == "taeyoon"


def test_batch_with_nothing_ready_is_409(client: TestClient, monkeypatch) -> None:
    calls = _fake_safety(monkeypatch, HELD)
    for seller in ("sel_0001", "sel_0003"):  # 위험 판매자 정산 보류 기록
        handlers.state().ledger.market_ops.append(
            {"skill": "settlement_hold", "seller_id": seller, "status": "held"}
        )
    response = client.post("/api/finance/settlements/batch", json={})
    assert (response.status_code, response.json()["error"]["code"]) == (409, "nothing_to_settle")
    assert calls == []
    rows = client.get("/api/finance/settlements").json()["data"]["statements"]
    assert all(r["status"] == "held" for r in rows)
    assert client.post("/api/finance/settlements/batch", json={"period": 42}).status_code == 400


def test_rejected_batch_can_be_requested_again(client: TestClient, monkeypatch) -> None:
    rejected = ExecuteDecision(
        execution_id="exe_5", grade="C", status="rejected", hold_reason=HoldReason.REVIEW_TIMEOUT
    )
    calls = _fake_safety(monkeypatch, rejected)
    first = client.post("/api/finance/settlements/batch", json={}).json()["data"]
    assert first["batch"]["status"] == "rejected"
    _fake_safety(monkeypatch, HELD)
    second = client.post("/api/finance/settlements/batch", json={}).json()["data"]
    assert second["batch"]["status"] == "requested"
    assert calls[0]["key"].endswith(":1")


def test_batch_status_follows_safety_review(client: TestClient, monkeypatch) -> None:
    _fake_safety(monkeypatch, HELD)
    client.post("/api/finance/settlements/batch", json={})
    monkeypatch.setenv("AX_SAFETY_URL", "http://safety")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": {"review_id": "rev_77", "status": "approved"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        finance_api.httpx, "AsyncClient", lambda: real(transport=httpx.MockTransport(handler))
    )
    data = client.get("/api/finance/settlements").json()["data"]
    assert data["batch"]["status"] == "approved" and seen == ["/api/safety/reviews/rev_77"]
    rows = {r["seller_id"]: r["status"] for r in data["statements"]}
    assert rows == {"sel_0001": "approved", "sel_0002": "held", "sel_0003": "approved"}


# ------------------------------------------------- CORS (대시보드가 브라우저에서 직접)


def test_dashboard_can_post_decisions_only_on_decision_paths(client: TestClient) -> None:
    for path in (
        "/api/finance/closing/confirm",
        "/api/finance/reconciliation/pay_1/note",
        "/api/finance/settlements/batch",
    ):
        preflight = client.options(
            path,
            headers={
                "Origin": "http://localhost:3001",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200, path
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:3001"
        assert "POST" in preflight.headers["access-control-allow-methods"]
    get = client.get("/api/finance/overview", headers={"Origin": "http://localhost:3001"})
    assert get.headers.get("access-control-allow-origin") == "http://localhost:3001"
    evil = client.get("/api/finance/overview", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in evil.headers
    a2a = client.options(
        "/a2a",
        headers={"Origin": "http://localhost:3001", "Access-Control-Request-Method": "POST"},
    )
    assert a2a.status_code == 400


def test_removed_endpoints(client: TestClient) -> None:
    for path in (
        "/api/finance/verifications",
        "/api/finance/ai-reviews",
        "/api/finance/catbooks/sync",
        "/api/finance/catbooks/items",
        "/api/finance/threshold-reviews",
        "/api/finance/loss-reports",  # 2026-10-27 장애 흐름에서 빠지며 없앰
    ):
        assert client.get(path).status_code == 404
    for path in (
        "/api/finance/ledger",
        "/api/finance/market-ops",
        "/api/finance/self-test/last",
    ):
        assert client.get(path).status_code == 200, path


def test_seed_dates_move_with_today(client: TestClient, monkeypatch) -> None:
    from finance_agent import finance_config as fc

    later = fc.now() + timedelta(days=10)
    monkeypatch.setattr(fc, "now", lambda: later)
    data = client.get("/api/finance/overview").json()["data"]
    assert data["date"] == later.date().isoformat() and data["kpis"]["gmv"] > 0
