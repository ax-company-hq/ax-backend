import asyncio
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, RequestMeta, TaskStatus
from ax_orchestrator.main import app, state
from ax_orchestrator.mock.harness import Harness, new_incident
from ax_orchestrator.models import now
from ax_orchestrator.plans.scenario_a import scenario_a
from ax_orchestrator.usage import CallRecord, RecordingCaller, UsageRecorder


def _req(skill: str = "impact_analysis", key: str | None = "k1") -> AgentRequest:
    meta = RequestMeta(
        context_id="ctx_inc_u",
        token="t",
        deadline=now() + timedelta(seconds=30),
        idempotency_key=key,
    )
    return AgentRequest(meta=meta, skill=skill)


def _rec(dept="data", skill="impact_analysis", status="completed", ms=100, ago=5.0, key=None):
    return CallRecord(
        dept, skill, now() - timedelta(seconds=ago), ms, status, None, "ctx_inc_u", key
    )


def test_summary_counts_success_rate_latency_and_retries() -> None:
    usage = UsageRecorder()
    usage.add(_rec(ms=100, key="a"))
    usage.add(_rec(ms=300, status="failed", key="b"))
    usage.add(_rec(ms=200, key="b"))  # 같은 멱등 키 = 재시도 1회
    usage.add(_rec(status="input-required", ms=50, key="c"))  # 대기는 성공률에서 뺀다
    usage.add(_rec(dept="hr"))
    s = usage.summary("data")
    assert (s["calls"], s["errors"], s["waiting"], s["retries"]) == (4, 1, 1, 1)
    assert s["success_rate"] == pytest.approx(0.667, abs=0.001)
    assert s["avg_ms"] == 162 and s["p95_ms"] == 300
    assert usage.summary("finance")["success_rate"] is None  # 호출이 없으면 지어내지 않는다


def test_old_calls_fall_out_of_the_window_and_series_has_fixed_buckets() -> None:
    usage = UsageRecorder()
    usage.add(_rec(ago=5))
    usage.add(_rec(ago=3600))  # 1시간 전
    assert usage.summary("data")["calls"] == 1
    series = usage.series("data")
    assert len(series) == 20 and sum(b["calls"] for b in series) == 1
    assert all(b["avg_ms"] is None for b in series if b["calls"] == 0)


def test_per_skill_summary() -> None:
    usage = UsageRecorder()
    usage.add(_rec(skill="a"))
    usage.add(_rec(skill="b"))
    assert usage.summary("data", skill="a")["calls"] == 1


@pytest.mark.asyncio
async def test_recording_caller_records_result_and_exception() -> None:
    usage = UsageRecorder()

    async def inner(dept, req):
        if req.skill == "boom":
            raise RuntimeError("down")
        await asyncio.sleep(0.01)
        return AgentResult(meta=req.meta, status=TaskStatus.COMPLETED)

    caller = RecordingCaller(inner, usage)
    assert (await caller("data", _req())).status == TaskStatus.COMPLETED
    with pytest.raises(RuntimeError):
        await caller("data", _req("boom", "k2"))
    s = usage.summary("data")
    assert (s["calls"], s["errors"]) == (2, 1)
    assert usage.recent("data")[0]["status"] == "error"
    assert usage.recent("data")[1]["duration_ms"] >= 10


@pytest.mark.asyncio
async def test_scenario_records_every_call_per_dept(harness: Harness) -> None:
    engine = await harness.start()
    usage = UsageRecorder()
    engine.caller = RecordingCaller(engine.caller, usage)
    await engine.run(new_incident(), scenario_a())
    assert usage.summary("marketing")["calls"] == 4
    assert usage.summary("hr")["calls"] == 1
    assert usage.summary("data")["calls"] == 2  # 영향 분석 + 처리 시간 요약(kpi_summary)


def _fresh_state(monkeypatch) -> None:
    monkeypatch.setattr(state, "mock", True)
    monkeypatch.setattr(state, "latency", 0.0)
    monkeypatch.setattr(state, "manager", None)
    monkeypatch.setattr(state, "usage", UsageRecorder())


def _wait_done(client, ctx: str) -> None:
    for _ in range(300):
        if client.get(f"/api/incidents/{ctx}").json()["data"]["status"] != "open":
            return
        time.sleep(0.02)


def test_departments_endpoint_exposes_usage_after_an_incident(monkeypatch) -> None:
    _fresh_state(monkeypatch)
    with TestClient(app) as client:
        before = client.get("/api/departments").json()
        assert [d["name"] for d in before["data"]][:3] == ["infra", "data", "planning"]
        assert all(d["usage"]["calls"] == 0 for d in before["data"])
        created = client.post("/api/demo/incidents", json={"fail_marketing": True})
        ctx = created.json()["data"]["context_id"]
        _wait_done(client, ctx)
        after = {d["name"]: d for d in client.get("/api/departments").json()["data"]}
    marketing = after["marketing"]
    assert marketing["usage"]["calls"] == 5 and marketing["usage"]["retries"] == 1
    publish = next(s for s in marketing["skills"] if s["id"] == "publish_notice")
    assert publish["calls"] == 3 and publish["errors"] == 1
    assert len(marketing["series"]) == 20 and marketing["recent"][0]["skill"] == "publish_notice"
    assert after["hr"]["usage"]["calls"] == 1


def test_departments_marks_unavailable_dept(monkeypatch) -> None:
    _fresh_state(monkeypatch)
    with TestClient(app) as client:
        client.post("/api/demo/incidents", json={"infra_card_empty": True})
        depts = client.get("/api/departments").json()["data"]
    infra = next(d for d in depts if d["name"] == "infra")
    assert infra["available"] is False and infra["skills"] == []
    assert "카드" in infra["unavailable_reason"]
