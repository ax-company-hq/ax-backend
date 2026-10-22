"""'재무 AI 테스트' 버튼(자체 점검) 테스트."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from finance_agent import handlers, selftest
from finance_agent.handlers import recompute
from finance_agent.main import app
from finance_agent.review import load_policy
from finance_agent.sources import MockSources

REGISTRY = {
    "actions": [
        {"action": "issue_coupon", "verifier": "finance"},
        {"action": "refund_request", "verifier": "finance"},
    ]
}
THRESHOLDS = {"auto_review": {"enabled": True, "reviewer": "finance"}}


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    events.reset_mock_bus()
    handlers.reset_state(MockSources())


def _network(up: set[str], registry: dict | None = None, thresholds: dict | None = None):
    """가짜 네트워크. `up`에 있는 서비스만 응답한다."""

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host not in up:
            raise httpx.ConnectError("down")
        if path == "/api/safety/registry":
            return httpx.Response(200, json={"data": registry or REGISTRY, "meta": {}})
        if path == "/api/safety/thresholds":
            return httpx.Response(200, json={"data": thresholds or THRESHOLDS, "meta": {}})
        return httpx.Response(200, json={"data": {"status": "ok"}, "meta": {}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _run(client: httpx.AsyncClient) -> dict[str, Any]:
    return await selftest.run_self_test(recompute, load_policy(), http_client=client)


@pytest.mark.asyncio
async def test_all_green_when_everything_is_up() -> None:
    result = await _run(_network({"safety", "shop-api", "data"}))
    assert result["result"] == "pass", [c for c in result["checks"] if not c["ok"]]
    assert result["passed"] == result["total"] >= 14
    names = {c["name"] for c in result["checks"]}
    assert {"금액을 틀리게 적은 쿠폰 막기", "결제보다 큰 환불 막기", "안전 장치 연결"} <= names


@pytest.mark.asyncio
async def test_missing_data_sources_only_warn() -> None:
    result = await _run(_network({"safety"}))
    assert result["result"] == "warn"
    bad = {c["name"]: c["level"] for c in result["checks"] if not c["ok"]}
    assert bad == {"쇼핑몰 연결": "warn", "데이터 AI 연결": "warn"}


@pytest.mark.asyncio
async def test_safety_down_fails() -> None:
    result = await _run(_network({"shop-api", "data"}))
    assert result["result"] == "fail"
    failed = {c["name"] for c in result["checks"] if not c["ok"] and c["level"] == "fail"}
    assert {"안전 장치 연결", "쿠폰·환불 검증을 재무 AI가 맡는지"} <= failed


@pytest.mark.asyncio
async def test_wrong_routing_and_auto_review_off_are_reported() -> None:
    registry = {
        "actions": [
            {"action": "issue_coupon", "verifier": "data"},
            {"action": "refund_request", "verifier": "finance"},
        ]
    }
    result = await _run(
        _network({"safety", "shop-api", "data"}, registry, {"auto_review": {"enabled": False}})
    )
    by = {c["name"]: c for c in result["checks"]}
    assert not by["쿠폰·환불 검증을 재무 AI가 맡는지"]["ok"]
    assert (by["AI 1차 판단 켜짐"]["ok"], by["AI 1차 판단 켜짐"]["level"]) == (False, "warn")
    assert result["result"] == "fail"


@pytest.mark.asyncio
async def test_broken_judgement_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """판단 코드가 고장 나면(무조건 통과) 테스트가 빨간불을 켠다."""
    from finance_agent.schemas import VerifyAmountOutput

    async def always_approve(*args: Any, **kwargs: Any) -> VerifyAmountOutput:
        return VerifyAmountOutput(decision="approved", reason_code="recomputed_match")

    result = await selftest.run_self_test(
        always_approve, load_policy(), http_client=_network({"safety", "shop-api", "data"})
    )
    failed = {c["name"] for c in result["checks"] if not c["ok"]}
    assert "금액을 틀리게 적은 쿠폰 막기" in failed and "결제보다 큰 환불 막기" in failed
    assert result["result"] == "fail"


@pytest.mark.asyncio
async def test_self_test_leaves_no_trace_in_real_records() -> None:
    await _run(_network({"safety", "shop-api", "data"}))
    ledger = handlers.state().ledger
    assert ledger.verify_log == [] and ledger.ai_reviews == [] and ledger.refunds == {}


def test_endpoints_run_and_remember_last(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(*args: Any, **kwargs: Any) -> dict:
        return {
            "result": "pass",
            "summary": "ok",
            "checks": [],
            "passed": 0,
            "total": 0,
            "duration_ms": 1,
            "ran_at": "2026-10-22T00:00:00+00:00",
        }

    monkeypatch.setattr("finance_agent.main.run_self_test", fake_run)
    client = TestClient(app)
    assert client.get("/api/finance/self-test/last").json()["data"] is None
    ran = client.get("/api/finance/self-test", headers={"Origin": "http://localhost:3001"})
    assert ran.json()["data"]["result"] == "pass"
    assert ran.headers.get("access-control-allow-origin") == "http://localhost:3001"
    assert client.get("/api/finance/self-test/last").json()["data"]["result"] == "pass"


def test_last_result_expires_after_12_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """아침마다 새로 테스트하도록 12시간이 지난 결과는 지운다."""
    from datetime import UTC, datetime, timedelta

    client = TestClient(app)
    old = (datetime.now(UTC) - timedelta(hours=12, minutes=1)).isoformat()
    handlers.state().last_self_test = {"result": "pass", "ran_at": old}
    body = client.get("/api/finance/self-test/last").json()
    assert body["data"] is None and body["meta"]["ttl_hours"] == 12
    assert handlers.state().last_self_test is None

    fresh = (datetime.now(UTC) - timedelta(hours=11)).isoformat()
    handlers.state().last_self_test = {"result": "fail", "ran_at": fresh}
    assert client.get("/api/finance/self-test/last").json()["data"]["result"] == "fail"


@pytest.mark.parametrize("bad", [{}, {"ran_at": "어제"}, {"ran_at": None}])
def test_strange_timestamps_count_as_old(bad: dict) -> None:
    assert selftest.is_fresh(bad) is False
