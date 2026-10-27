"""시나리오 A 재무 몫 테스트: 사건별 쿠폰 장부(⑨)·손실 리포트(⑩)·사건 후 감사(⑰), 재설계 문서 12장.

네트워크 없음. 실제 모드는 가짜 안전 관문(httpx MockTransport)과 가짜 data(`call_agent`)로 본다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ax_agent_template import events
from ax_contracts.schemas import (
    AgentRequest,
    AgentResult,
    EventEnvelope,
    EventType,
    TaskStatus,
)
from finance_agent import audit, handlers, incidents, sources
from finance_agent import ledger as lg
from finance_agent.main import app
from finance_agent.sources import LiveSources, MockSources

CTX = "ctx_inc_scn_a_0001"
OLD = "ctx_inc_0100"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
THRESHOLDS = {
    "version": 1,
    "rules": [
        {"key": "compensation_per_context", "action": "issue_coupon", "op": ">=", "value": 1000000},
        {"key": "compensation_per_customer", "action": "issue_coupon", "op": ">=", "value": 50000},
    ],
}
IMPACT = {
    "failed_order_count": 6,
    "affected_customer_count": 4,
    "damage_amount": 95000,
    "affected_customer_ids": ["buy_0001", "buy_0002", "buy_0003", "buy_0004"],
    "by_category": [
        {"category": "books", "failed_order_count": 4, "damage_amount": 82500},
        {"category": "stationery", "failed_order_count": 2, "damage_amount": 12500},
    ],
    "summary": "결제 실패 6건",
}
LIST_KEYS = {
    "context_id",
    "component",
    "opened_at",
    "damage_amount",
    "compensation_total",
    "coupon_count",
    "held_count",
    "checks_ok",
}
DETAIL_KEYS = {
    "context_id",
    "component",
    "window",
    "impact",
    "compensation",
    "limits",
    "checks",
    "ledger_source",
}
COUPON_KEYS = {
    "buyer_id",
    "execution_id",
    "coupon_id",
    "count",
    "unit_value",
    "amount",
    "purpose_code",
    "reason_code",
    "grade",
    "status",
    "review_id",
    "at",
    "in_impact",
}
CHECK_CODES = [
    "recipients_in_impact",
    "one_coupon_per_buyer",
    "per_customer_limit",
    "per_incident_limit",
    "amount_recheck",
]


def _exe(
    exe: str,
    buyer: str,
    *,
    ctx: str = CTX,
    status: str = "executed",
    count: int = 1,
    unit: int = 2000,
    total: int | None = None,
    computed: int | None = None,
    purpose: str = "compensation",
    key: str | None = None,
    minute: int = 0,
    grade: str = "A",
    review_id: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """장부 모양(normalize 뒤)의 실행 기록."""
    return {
        "execution_id": exe,
        "created_at": NOW - timedelta(hours=2) + timedelta(minutes=minute),
        "action": "issue_coupon",
        "requester": "marketing",
        "context_id": ctx,
        "params": {
            "buyer_id": buyer,
            "purpose_code": purpose,
            "reason_code": "payment_failure",
            "count": count,
            "unit_value": unit,
            "total_value": total if total is not None else count * unit,
        },
        "status": status,
        "grade": grade,
        "hold_reason": None if status != "held" else "over_threshold",
        "computed_amount": computed if computed is not None else count * unit,
        "review_id": review_id,
        "result_ref": ref,
        "idempotency_key": key or f"{ctx}:issue_coupon:{buyer}:{exe}",
    }


def _raw_record(e: dict[str, Any]) -> dict[str, Any]:
    """safety `ExecutionRecord` JSON 모양."""
    return {
        "execution_id": e["execution_id"],
        "created_at": e["created_at"].isoformat(),
        "request": {
            "action": e["action"],
            "requester": e["requester"],
            "context_id": e["context_id"],
            "tenant_id": "shop-demo",
            "idempotency_key": e["idempotency_key"],
            "params": e["params"],
        },
        "decision": {
            "execution_id": e["execution_id"],
            "grade": e["grade"],
            "status": e["status"],
            "computed_amount": e["computed_amount"],
            "hold_reason": e["hold_reason"],
            "review_id": e["review_id"],
            "result_ref": e["result_ref"],
        },
    }


def _feed(rows: list[dict[str, Any]] | None, source: str = lg.LEDGER_SAFETY) -> lg.ExecutionFeed:
    return lg.ExecutionFeed(rows, source)


def _event(n: int, ctx: str, total: int, **extra: Any) -> EventEnvelope:
    return EventEnvelope(
        event_id=f"evt_i{n}",
        type=EventType.COUPON_ISSUED,
        occurred_at=NOW,
        context_id=ctx,
        source="marketing",
        idempotency_key=f"coupon.issued:cpn_i{n}",
        data={
            "coupon_id": f"cpn_i{n}",
            "campaign_id": None,
            "purpose_code": "compensation",
            "count": 1,
            "total_value": total,
            **extra,
        },
    )


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch, fixed_now) -> None:
    events.reset_mock_bus()
    monkeypatch.setitem(lg.EVENTS, "connected", False)
    handlers.reset_state(MockSources())


@pytest.fixture
def mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv("AX_SAFETY_URL", raising=False)


@pytest.fixture
def real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI가 켜 두는 목 모드 변수를 끈다."""
    monkeypatch.delenv("AGENT_TEMPLATE_MOCK", raising=False)
    monkeypatch.delenv("FINANCE_MOCK", raising=False)
    monkeypatch.setenv("AX_SAFETY_URL", "http://safety")


def _safety(
    records: list[dict[str, Any]] | None, seen: list[httpx.Request] | None = None
) -> httpx.AsyncClient:
    """가짜 안전 관문. records가 None이면 꺼져 있다."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if records is None:
            raise httpx.ConnectError("safety down")
        if request.url.path == "/api/safety/executions":
            ctx = request.url.params.get("context_id")
            rows = [r for r in records if ctx is None or r["request"]["context_id"] == ctx]
            return httpx.Response(200, json={"data": rows, "meta": {}})
        if request.url.path == "/api/safety/thresholds":
            return httpx.Response(200, json={"data": THRESHOLDS, "meta": {}})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _use_safety(client: httpx.AsyncClient) -> None:
    s = handlers.state()

    async def executions(ctx: str | None) -> lg.ExecutionFeed:
        return await lg.load_executions(ctx, http_client=client)

    async def thresholds() -> dict[str, Any] | None:
        return await lg.load_thresholds(http_client=client)

    s.executions_loader = executions
    s.thresholds_loader = thresholds


@pytest.fixture
def fake_data(monkeypatch: pytest.MonkeyPatch) -> list[AgentRequest]:
    calls: list[AgentRequest] = []

    async def fake_call_agent(name: str, request: AgentRequest) -> AgentResult:
        assert name == "data" and request.skill == "impact_analysis"
        calls.append(request)
        return AgentResult(meta=request.meta, status=TaskStatus.COMPLETED, output=dict(IMPACT))

    monkeypatch.setattr(sources, "call_agent", fake_call_agent)
    return calls


def _detail(ctx: str = CTX) -> dict[str, Any]:
    """지금 상태의 실행 기록으로 만든 사건 상세 data."""
    feed = asyncio.run(handlers.state().executions_loader(ctx))
    data, _ = asyncio.run(incidents.incident_view(ctx, feed, None))
    return data


# ---------------------------------------------------------------- ⑨ 장부 줄과 중복 방지
def test_coupon_rows_only_compensation_from_gate() -> None:
    rows = [
        _exe("exe_1", "buy_0001", ref="cpn_1"),
        _exe("exe_2", "buy_0002", status="held", grade="C", review_id="rev_2"),
        _exe("exe_3", "buy_0003", status="rejected", grade="C"),
        _exe("exe_4", "buy_0004", purpose="campaign"),  # 캠페인은 보상 장부가 아님
        _exe("exe_5", "buy_0005", status="verifying"),  # 결과가 아직 없음
        _exe("exe_6", "buy_0006", ctx="ctx_inc_other"),  # 다른 사건
    ]
    out = lg.execution_coupons(rows, CTX)
    assert [r["execution_id"] for r in out] == ["exe_1", "exe_2", "exe_3"]
    first = out[0]
    assert (first["coupon_id"], first["amount"], first["grade"], first["reason_code"]) == (
        "cpn_1",
        2000,
        "A",
        "payment_failure",
    )
    assert out[1]["review_id"] == "rev_2"
    totals = lg.totals(out)
    assert totals == {
        "total_executed": 2000,
        "total_held": 2000,
        "count_executed": 1,
        "count_held": 1,
    }


def test_execution_rows_dedupe_by_execution_and_idempotency_key() -> None:
    same_key = f"{CTX}:issue_coupon:buy_0002"
    rows = [
        _exe("exe_1", "buy_0001"),
        _exe("exe_1", "buy_0001"),  # 같은 실행 기록이 두 번 와도 한 줄
        _exe("exe_2", "buy_0002", key=same_key, status="held", grade="C"),
        _exe("exe_3", "buy_0002", key=same_key, minute=5),  # 같은 요청 다시 보내기 → 마지막만
    ]
    out = lg.execution_coupons(rows, CTX)
    assert [r["execution_id"] for r in out] == ["exe_1", "exe_3"]
    assert lg.totals(out)["total_executed"] == 4000


def test_merge_prefers_gate_records_and_never_double_counts_events() -> None:
    gate = [_exe("exe_1", "buy_0001"), _exe("exe_2", "buy_0002")]
    event_rows = [
        lg.coupon_from_event(CTX, {"coupon_id": "cpn_a", "count": 1, "total_value": 2000}, NOW),
        lg.coupon_from_event(
            CTX,
            {"coupon_id": "cpn_b", "count": 1, "total_value": 2000, "execution_id": "exe_2"},
            NOW,
        ),
    ]
    rows, source = lg.merge(CTX, _feed(gate), event_rows, events_available=True)
    assert source == "safety_executions"
    assert rows is not None and lg.totals(rows)["total_executed"] == 4000  # 6,000이 아니다
    assert [r["coupon_id"] for r in rows] == [None, "cpn_b"]  # 이벤트는 coupon_id만 채운다


def test_merge_falls_back_to_events_then_fails_closed() -> None:
    event_rows = [
        lg.coupon_from_event(CTX, {"coupon_id": "cpn_a", "count": 2, "total_value": 4000}, NOW)
    ]
    rows, source = lg.merge(CTX, _feed([]), event_rows, events_available=False)
    assert source == "events" and rows is not None
    assert (rows[0]["unit_value"], rows[0]["status"], rows[0]["buyer_id"]) == (
        2000,
        "executed",
        None,
    )
    assert lg.merge(CTX, _feed([]), [], events_available=False) == ([], "safety_executions")
    assert lg.merge(CTX, _feed(None), [], events_available=True) == ([], "events")
    assert lg.merge(CTX, _feed(None), [], events_available=False) == (None, None)
    assert lg.merge(CTX, _feed([], lg.LEDGER_SEED), [], False) == ([], "seed")


def test_load_executions_reads_gate_records(real_mode) -> None:
    seen: list[httpx.Request] = []
    rec = _raw_record(_exe("exe_9", "buy_0001", ref="cpn_9"))
    feed = asyncio.run(lg.load_executions(CTX, http_client=_safety([rec], seen)))
    assert feed.source == "safety_executions" and feed.rows is not None
    assert seen[0].method == "GET" and seen[0].url.path == "/api/safety/executions"
    assert seen[0].url.params["context_id"] == CTX
    [row] = lg.execution_coupons(feed.rows, CTX)
    assert (row["coupon_id"], row["buyer_id"], row["amount"]) == ("cpn_9", "buy_0001", 2000)
    down = asyncio.run(lg.load_executions(CTX, http_client=_safety(None)))
    assert down.rows is None
    assert asyncio.run(lg.load_thresholds(http_client=_safety(None))) is None


# ------------------------------------------------------------ 보상 쿠폰 장부 (실제 모드)
def test_compensation_total_in_real_mode_comes_from_gate_records(real_mode, fake_data) -> None:
    handlers.reset_state(LiveSources())
    records = [_raw_record(_exe(f"exe_{n}", f"buy_000{n}")) for n in range(1, 5)]
    records.append(_raw_record(_exe("exe_9", "buy_0009", ctx="ctx_inc_other")))
    _use_safety(_safety(records))
    data = _detail()
    comp = data["compensation"]
    # 다른 사건(ctx_inc_other)의 쿠폰은 섞이지 않는다
    assert (comp["count_executed"], comp["total_executed"]) == (4, 8000)
    assert data["ledger_source"] == "safety_executions"
    assert [c["execution_id"] for c in comp["coupons"]] == ["exe_1", "exe_2", "exe_3", "exe_4"]
    # 장애 구간을 알려 주던 loss_aggregation을 없앴다 → data에 묻지 않는다
    assert data["impact"] is None and fake_data == []


def test_compensation_without_any_ledger_source_is_unknown(real_mode, fake_data) -> None:
    handlers.reset_state(LiveSources())
    _use_safety(_safety(None))  # 안전 관문 꺼짐 + 이벤트 버스 없음
    data, unavailable = asyncio.run(incidents.incident_view(CTX, _feed(None), None))
    comp = data["compensation"]
    # 0으로 바꾸지 않고 "확인할 수 없음"(None)으로 둔다
    assert (comp["count_executed"], comp["total_executed"]) == (None, None)
    assert "compensation" in unavailable and data["ledger_source"] is None
    assert all(c["ok"] is None for c in data["checks"])


def test_compensation_uses_events_when_no_gate_record_and_does_not_double_count(mock_mode) -> None:
    handlers.reset_state(MockSources())
    # 시드 실행 기록이 있는 사건: 같은 쿠폰의 이벤트가 와도 더하지 않는다
    asyncio.run(handlers.on_event(_event(1, CTX, 2000, execution_id="exe_s0060")))
    data = _detail()
    assert (data["compensation"]["total_executed"], data["ledger_source"]) == (8000, "seed")
    # 실행 기록이 없는 사건은 이벤트 장부
    asyncio.run(handlers.on_event(_event(2, "ctx_inc_0001", 5000)))
    other = _detail("ctx_inc_0001")
    assert (other["compensation"]["total_executed"], other["ledger_source"]) == (5000, "events")


# ---------------------------------------------------------------- 사건 API 형식 (목 모드)
def test_incident_list_shape_and_numbers(mock_mode) -> None:
    body = TestClient(app).get("/api/finance/incidents").json()
    assert set(body) == {"data", "meta"}
    assert body["meta"]["mock"] is True and body["meta"]["ledger_source"] == "seed"
    rows = {r["context_id"]: r for r in body["data"]}
    for r in body["data"]:
        assert set(r) == LIST_KEYS and r["context_id"].startswith("ctx_inc_")
    cur = rows[CTX]
    assert cur == {
        "context_id": CTX,
        "component": "payment",
        "opened_at": "2026-10-22T05:00:00Z",
        "damage_amount": 95000,
        "compensation_total": 8000,
        "coupon_count": 4,
        "held_count": 0,
        "checks_ok": True,
    }
    assert rows[OLD]["checks_ok"] is False  # 영향 고객이 아닌 buy_0999
    assert body["data"][0]["context_id"] == CTX  # 최근 사건 먼저


def test_incident_detail_shape_matches_spec(mock_mode) -> None:
    body = TestClient(app).get(f"/api/finance/incidents/{CTX}").json()
    data, meta = body["data"], body["meta"]
    assert set(data) == DETAIL_KEYS and set(meta) >= {"mock", "unavailable"}
    assert meta["unavailable"] == [] and data["ledger_source"] == "seed"
    assert data["window"] == {"from": "2026-10-22T05:00:00Z", "to": "2026-10-22T05:30:00Z"}
    impact = data["impact"]
    assert set(impact) == {
        "failed_orders",
        "affected_customers",
        "damage_amount",
        "by_category",
        "affected_customer_ids",
        "source",
        "collected_at",
    }
    assert (impact["failed_orders"], impact["affected_customers"], impact["damage_amount"]) == (
        6,
        4,
        95000,
    )
    assert impact["by_category"] == [
        {"category": "books", "orders": 4, "amount": 82500},
        {"category": "stationery", "orders": 2, "amount": 12500},
    ]
    assert impact["affected_customer_ids"] == ["buy_0001", "buy_0002", "buy_0003", "buy_0004"]
    assert impact["source"] == "data.impact_analysis"
    comp = data["compensation"]
    assert set(comp) == {"coupons", "total_executed", "total_held", "count_executed", "count_held"}
    assert (comp["total_executed"], comp["count_executed"], comp["total_held"]) == (8000, 4, 0)
    for c in comp["coupons"]:
        assert set(c) == COUPON_KEYS
        assert (c["unit_value"], c["reason_code"], c["grade"], c["status"]) == (
            2000,
            "payment_failure",
            "A",
            "executed",
        )
        assert c["in_impact"] is True and c["coupon_id"].startswith("cpn_")
    assert data["limits"] == {
        "per_customer": {"limit": 50000, "max_used": 2000, "buyer_id": "buy_0001"},
        "per_incident": {"limit": 1000000, "used": 8000, "remaining": 992000},
    }
    # 재무는 장애 대응 흐름의 단계가 아니다 (2026-10-27): 손실 집계·단계 표시를 뺐다
    assert "loss" not in data and "steps" not in data
    assert [c["code"] for c in data["checks"]] == CHECK_CODES
    for c in data["checks"]:
        assert set(c) == {"code", "ok", "title", "detail", "refs"} and c["ok"] is True
    # 사람이 읽는 문장에는 영어 코드 대신 쉬운 말 (코드는 code·ledger_source에만)
    for c in data["checks"]:
        assert c["code"] not in c["title"] + c["detail"] and "⊂" not in c["title"]


def test_unknown_incident_is_404(mock_mode) -> None:
    response = TestClient(app).get("/api/finance/incidents/ctx_inc_9999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------- 점검 하나씩 (참·거짓)
def _view(rows: list[dict[str, Any]] | None, thresholds: dict | None = THRESHOLDS) -> dict:
    data, _ = asyncio.run(incidents.incident_view(CTX, _feed(rows), thresholds))
    return {c["code"]: c for c in data["checks"]}


def _ok(rows: list[dict[str, Any]] | None, thresholds: dict | None = THRESHOLDS) -> dict:
    return {code: c["ok"] for code, c in _view(rows, thresholds).items()}


GOOD = [_exe(f"exe_{n}", f"buy_000{n}", minute=n) for n in range(1, 5)]


def test_all_checks_pass_for_clean_incident(mock_mode) -> None:
    assert _ok(GOOD) == dict.fromkeys(CHECK_CODES, True)


def test_recipients_in_impact_false_and_unknown(mock_mode) -> None:
    checks = _view([*GOOD, _exe("exe_9", "buy_0999")])
    assert checks["recipients_in_impact"]["ok"] is False
    assert checks["recipients_in_impact"]["refs"] == ["exe_9"]
    handlers.reset_state(MockSources({"impacts": {}}))  # 영향 분석을 못 받음
    assert _ok(GOOD)["recipients_in_impact"] is None


def test_one_coupon_per_buyer_false(mock_mode) -> None:
    checks = _view([*GOOD, _exe("exe_5", "buy_0001", minute=9)])
    assert checks["one_coupon_per_buyer"]["ok"] is False
    assert set(checks["one_coupon_per_buyer"]["refs"]) == {"exe_1", "exe_5"}


def test_per_customer_limit_false_at_threshold_and_unknown_without_thresholds(mock_mode) -> None:
    big = [_exe("exe_1", "buy_0001", count=25, unit=2000)]  # 50,000원 = 기준표 `>=` 한도
    checks = _view(big)
    assert checks["per_customer_limit"]["ok"] is False
    assert checks["per_customer_limit"]["refs"] == ["exe_1"]
    assert _ok(big, None)["per_customer_limit"] is None
    assert _ok(big, None)["per_incident_limit"] is None


def test_per_incident_limit_false(mock_mode) -> None:
    tight = {
        "rules": [
            {"key": "compensation_per_context", "op": ">=", "value": 8000},
            {"key": "compensation_per_customer", "op": ">=", "value": 50000},
        ]
    }
    assert _ok(GOOD, tight)["per_incident_limit"] is False
    loose = {"rules": [{"key": "compensation_per_context", "op": ">", "value": 8000}]}
    assert _ok(GOOD, loose)["per_incident_limit"] is True  # `>`면 같은 금액은 통과


def test_amount_recheck_false(mock_mode) -> None:
    wrong = [*GOOD[:3], _exe("exe_4", "buy_0004", total=3000)]  # 1장 × 2,000원인데 총액 3,000원
    checks = _view(wrong)
    assert checks["amount_recheck"]["ok"] is False and checks["amount_recheck"]["refs"] == ["exe_4"]


def test_held_coupons_are_listed_but_not_paid(mock_mode) -> None:
    rows = [*GOOD, _exe("exe_5", "buy_0001", status="held", grade="C", review_id="rev_5")]
    data, _ = asyncio.run(incidents.incident_view(CTX, _feed(rows), THRESHOLDS))
    comp = data["compensation"]
    assert (comp["total_executed"], comp["count_held"], comp["total_held"]) == (8000, 1, 2000)
    assert data["checks"][1]["ok"] is True  # 보류 건은 지급이 아니라 중복으로 치지 않는다


def test_no_ledger_source_makes_everything_unknown(real_mode) -> None:
    handlers.reset_state(MockSources())
    data, unavailable = asyncio.run(incidents.incident_view(CTX, _feed(None), None))
    assert {"compensation", "limits"} <= set(unavailable)
    assert data["compensation"]["total_executed"] is None and data["ledger_source"] is None
    assert all(c["ok"] is None for c in data["checks"])
    assert "loss" not in data and "steps" not in data
    assert "장부" in data["checks"][0]["detail"]  # 사람이 읽는 쉬운 말
    assert incidents.checks_ok(data["checks"]) is None


# ---------------------------------------------------------------- 실제 모드 사건 API
def test_real_mode_incident_uses_gate_records_and_has_no_window(real_mode, fake_data) -> None:
    handlers.reset_state(LiveSources())
    records = [_raw_record(e) for e in GOOD]
    _use_safety(_safety(records))
    client = TestClient(app)
    body = client.get(f"/api/finance/incidents/{CTX}").json()
    data = body["data"]
    assert body["meta"]["mock"] is False and data["ledger_source"] == "safety_executions"
    # 장애 구간을 알려 주던 loss_aggregation을 없앴다 → 실제 모드에서는 구간·영향이 늘 null이고
    # data에 묻지도 않는다 (재무는 장애 대응 흐름의 단계가 아니다, 요구사항 DEP-FIN-01)
    assert data["window"] is None
    assert data["impact"] is None and "impact" in body["meta"]["unavailable"] and fake_data == []
    assert data["limits"]["per_customer"]["limit"] == 50000  # 안전 관문 기준표에서 읽음
    assert data["compensation"]["count_executed"] == 4
    listing = client.get("/api/finance/incidents").json()
    assert listing["meta"]["ledger_source"] == "safety_executions"
    assert [r["context_id"] for r in listing["data"]] == [CTX]
    assert listing["data"][0]["damage_amount"] is None  # 영향을 모르면 피해 금액도 모른다


# ---------------------------------------------------------------- ⑰ 감사 finding
def _lookup(ids: list[str] | None):
    async def lookup(ctx: str):
        if ids is None:
            return None
        return sources.Impact(
            failed_orders=len(ids),
            affected_customers=len(ids),
            lost_amount=1000,
            affected_customer_ids=frozenset(ids),
        )

    return lookup


def test_audit_compensation_outside_impact(kit) -> None:
    rows = [
        _exe("exe_1", "buy_0001", ref="cpn_1"),
        _exe("exe_2", "buy_0999", ref="cpn_2"),
        _exe("exe_3", "buy_0888", status="held", grade="C"),  # 보류 건은 지급이 아니다
    ]
    books = kit.books(executions=rows)
    data, _ = asyncio.run(audit.audit_report(books, 7, impact_lookup=_lookup(["buy_0001"])))
    [found] = [f for f in data["findings"] if f["code"] == "compensation_outside_impact"]
    assert found["severity"] == "high" and "buy_0999" in found["detail"] and CTX in found["detail"]
    assert "buy_0888" not in found["detail"]
    assert "데이터 AI 영향 고객" in found["detail"] and "data " not in found["detail"]
    assert "compensation_outside_impact" not in found["title"] + found["detail"]
    assert found["refs"] == ["exe_2", "cpn_2"]
    assert data["findings"][0]["severity"] == "high"
    # 영향 분석이 없거나 lookup이 없으면 의심으로 만들지 않는다
    for lookup in (_lookup(None), None):
        data, _ = asyncio.run(audit.audit_report(books, 7, impact_lookup=lookup))
        assert "compensation_outside_impact" not in {f["code"] for f in data["findings"]}


def test_audit_context_filter(kit) -> None:
    rows = [_exe("exe_1", "buy_0999"), _exe("exe_2", "buy_0999", ctx=OLD)]
    books = kit.books(executions=rows)
    data, _ = asyncio.run(
        audit.audit_report(books, 7, impact_lookup=_lookup(["buy_0001"]), context_id=OLD)
    )
    [found] = data["findings"]
    assert OLD in found["detail"] and found["refs"] == ["exe_2"]


def test_audit_api_and_skill_show_new_finding(mock_mode) -> None:
    client = TestClient(app)
    body = client.get("/api/finance/audit", params={"days": 7, "context_id": OLD}).json()
    codes = {f["code"] for f in body["data"]["findings"]}
    assert codes == {"compensation_outside_impact"}
    [found] = body["data"]["findings"]
    assert "buy_0999" in found["detail"] and "exe_s0065" in found["refs"]
    clean = client.get("/api/finance/audit", params={"days": 7, "context_id": CTX}).json()
    assert clean["data"]["findings"] == []
    skill = client.post(
        "/a2a",
        json={
            "meta": {
                "context_id": OLD,
                "token": "mock:orchestrator",
                "path": [],
                "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            },
            "skill": "audit_report",
            "input": {"days": 7, "context_id": OLD},
            "untrusted": {},
        },
    ).json()
    assert [f["code"] for f in skill["output"]["findings"]] == ["compensation_outside_impact"]
