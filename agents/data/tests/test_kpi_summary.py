"""kpi_summary skill 테스트 (공통 로그 목·SQLite 저장소, LLM 목, 네트워크 없음)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import common_log
from ax_agent_template.common_log import SqliteLogStore
from ax_agent_template.llm import MockLLMClient
from ax_agent_template.server import create_agent_app
from ax_contracts.data_access.common_log import LogRecord
from ax_contracts.schemas import AgentCard
from ax_contracts.schemas.data import KpiSummaryInput
from data_agent import handlers, seed_logs
from data_agent.clients.log_client import CommonLogSource, MockLogSource, get_log_source
from data_agent.explain import _Summary
from data_agent.handlers import HANDLERS
from data_agent.metrics.kpi import UNVERIFIED_RECOVERY, compute_kpi
from data_agent.seed_logs import CTX_FIRST, CTX_SECOND, SEED_LOGS
from data_agent.skills.kpi_summary import summarize_kpi

asyncio = pytest.mark.asyncio
TENANT = "shop-demo"
EXPECTED_SENTENCE = "2차 사건은 회사 기억을 참조해 1차보다 3분 빨리 복구했습니다."


async def _kpi(llm: MockLLMClient | None = None, source=None, **kw):
    return await summarize_kpi(
        KpiSummaryInput(**kw), TENANT, llm or MockLLMClient(), source=source or MockLogSource()
    )


def _only(ctx: str, drop) -> list[LogRecord]:
    return [r for r in SEED_LOGS if r.context_id == ctx and not drop(r)]


def _is_resolved_event(r: LogRecord) -> bool:
    return r.kind == "event_published" and r.payload.get("event_type") == "incident.resolved"


def _is_verify(r: LogRecord) -> bool:
    return r.skill == "verify_recovery"


# ---------- 목 시드 정답값 ----------


@asyncio
async def test_matches_seed_answers() -> None:
    out = (await _kpi()).output
    assert [i.context_id for i in out.incidents] == [CTX_FIRST, CTX_SECOND]
    assert [i.mttr_seconds for i in out.incidents] == [
        seed_logs.EXPECTED_MTTR_FIRST,
        seed_logs.EXPECTED_MTTR_SECOND,
    ]
    assert tuple(i.used_memory for i in out.incidents) == seed_logs.EXPECTED_USED_MEMORY
    assert (
        tuple(i.resolved_notice_at for i in out.incidents) == seed_logs.EXPECTED_RESOLVED_NOTICE_AT
    )
    assert out.auto_handled_rate == seed_logs.EXPECTED_AUTO_HANDLED_RATE
    assert out.grade_counts == seed_logs.EXPECTED_GRADE_COUNTS
    assert out.blocked_counts == seed_logs.EXPECTED_BLOCKED_COUNTS
    assert out.escalation_count == seed_logs.EXPECTED_ESCALATION_COUNT


@asyncio
async def test_detected_at_is_first_log_and_mttr_matches() -> None:
    first, second = (await _kpi()).output.incidents
    assert first.detected_at == datetime(2026, 10, 21, 5, 0, tzinfo=UTC)
    for inc in (first, second):
        assert inc.recovered_at is not None
        assert inc.mttr_seconds == (inc.recovered_at - inc.detected_at).total_seconds()
    diff = (first.mttr_seconds - second.mttr_seconds) // 60
    assert diff == seed_logs.EXPECTED_FASTER_MINUTES


@asyncio
async def test_non_incident_records_are_excluded() -> None:
    """context_id 없는 차단과 `ctx_req_`의 등급 판정은 사건 합계에 안 들어간다."""
    out = (await _kpi()).output
    assert seed_logs.CTX_REQUEST not in [i.context_id for i in out.incidents]
    assert out.blocked_counts["UNAUTHORIZED"] == 1
    assert out.grade_counts["A"] == 3


@asyncio
async def test_context_ids_filter() -> None:
    out = (await _kpi(context_ids=[CTX_SECOND])).output
    assert [i.context_id for i in out.incidents] == [CTX_SECOND]
    assert out.auto_handled_rate == 1.0
    assert out.escalation_count == 0
    assert out.grade_counts == {"A": 2, "B": 1}


@asyncio
async def test_period_filter() -> None:
    out = (await _kpi(period_from=datetime(2026, 10, 22, tzinfo=UTC))).output
    assert [i.context_id for i in out.incidents] == [CTX_SECOND]


@asyncio
async def test_deterministic() -> None:
    assert (await _kpi()).output == (await _kpi()).output


# ---------- 설명 한 줄 (LLM) ----------


@asyncio
async def test_explanation_template_without_llm() -> None:
    result = await _kpi()  # 목 LLM 응답 없음 → 고정 템플릿
    assert result.summary == EXPECTED_SENTENCE
    assert result.fallback is True
    assert result.evidence.data_summary.splitlines()[0] == EXPECTED_SENTENCE


@asyncio
async def test_explanation_from_llm_when_numbers_match() -> None:
    text = "회사 기억을 참조한 2차 사건이 1차보다 3분 빨리 복구됐습니다."
    llm = MockLLMClient(responses={_Summary: _Summary(summary=text)})
    result = await _kpi(llm)
    assert (result.summary, result.fallback) == (text, False)


@asyncio
async def test_explanation_with_wrong_number_falls_back() -> None:
    llm = MockLLMClient(responses={_Summary: _Summary(summary="2차가 1차보다 5분 빨랐습니다.")})
    result = await _kpi(llm)
    assert (result.summary, result.fallback) == (EXPECTED_SENTENCE, True)


@asyncio
async def test_single_incident_skips_llm() -> None:
    llm = MockLLMClient()
    result = await _kpi(llm, context_ids=[CTX_FIRST])
    assert result.summary is None and llm.calls == []
    assert "1차·2차 비교 확인할 수 없음" in result.evidence.data_summary


# ---------- 로그가 비었을 때 ----------


@asyncio
async def test_empty_logs() -> None:
    result = await _kpi(period_from=datetime(2027, 1, 1, tzinfo=UTC))
    out = result.output
    assert out.incidents == []
    assert out.auto_handled_rate == 0.0
    assert out.grade_counts == {} and out.blocked_counts == {} and out.escalation_count == 0
    assert result.summary is None
    assert "확인할 수 없음: 조건에 맞는 공통 로그 0건" in result.evidence.data_summary


def test_missing_kinds_are_reported_not_guessed() -> None:
    """실제 모드처럼 등급·차단·이관 로그가 없으면 0이고 근거에 사유를 남긴다."""
    kinds = {"execute_decided", "rejected", "escalated"}
    computed = compute_kpi(_only(CTX_SECOND, lambda r: r.kind in kinds))
    out = computed.output
    assert (out.grade_counts, out.blocked_counts, out.escalation_count) == ({}, {}, 0)
    for name in ("grade_counts", "blocked_counts", "escalation_count"):
        assert f"{name}: 확인할 수 없음: 해당 종류 로그 0건" in "\n".join(computed.notes)


# ---------- 복구 판정 순서: incident.resolved → verify_recovery 완료 → None ----------


def test_recovery_prefers_incident_resolved() -> None:
    computed = compute_kpi(_only(CTX_FIRST, lambda r: False))
    incident = computed.output.incidents[0]
    assert incident.recovered_at == datetime(2026, 10, 21, 5, 9, tzinfo=UTC)  # verify는 05:08
    assert computed.recovery_basis[CTX_FIRST] == "incident.resolved"
    assert not any(UNVERIFIED_RECOVERY in n for n in computed.notes)


def test_recovery_falls_back_to_verify_completed() -> None:
    computed = compute_kpi(_only(CTX_FIRST, _is_resolved_event))
    incident = computed.output.incidents[0]
    assert incident.recovered_at == datetime(2026, 10, 21, 5, 8, tzinfo=UTC)
    assert incident.mttr_seconds == 480
    assert computed.recovery_basis[CTX_FIRST] == "verify_recovery"
    assert f"{CTX_FIRST}: {UNVERIFIED_RECOVERY}" in computed.notes


def test_failed_verify_is_not_recovery() -> None:
    records = [
        r.model_copy(update={"task_status": "failed"}) if _is_verify(r) else r
        for r in _only(CTX_FIRST, _is_resolved_event)
    ]
    assert compute_kpi(records).output.incidents[0].recovered_at is None


def test_no_recovery_log_gives_none() -> None:
    computed = compute_kpi(_only(CTX_SECOND, lambda r: _is_resolved_event(r) or _is_verify(r)))
    incident = computed.output.incidents[0]
    assert incident.recovered_at is None and incident.mttr_seconds is None
    assert computed.recovery_basis[CTX_SECOND] is None
    assert any("복구 시각 확인할 수 없음" in n for n in computed.notes)


def test_single_notice_gives_no_resolved_notice() -> None:
    notices = [r for r in SEED_LOGS if r.context_id == CTX_FIRST and r.skill == "publish_notice"]
    computed = compute_kpi(_only(CTX_FIRST, lambda r: r is notices[1]))
    assert computed.output.incidents[0].resolved_notice_at is None
    assert any("정상화 공지 확인할 수 없음" in n for n in computed.notes)


# ---------- 실제 저장소(SQLite) 읽기 ----------


@asyncio
async def test_reads_common_log_store() -> None:
    store = SqliteLogStore(":memory:")
    for record in SEED_LOGS:
        store.write(record)
    out = (await _kpi(source=CommonLogSource(store))).output
    assert [i.mttr_seconds for i in out.incidents] == [
        seed_logs.EXPECTED_MTTR_FIRST,
        seed_logs.EXPECTED_MTTR_SECOND,
    ]
    assert out.grade_counts == seed_logs.EXPECTED_GRADE_COUNTS
    store.close()


def test_get_log_source(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(get_log_source({"DATA_MOCK": "1"}), MockLogSource)
    common_log.set_store(None)
    monkeypatch.delenv(common_log.DB_ENV, raising=False)
    with pytest.raises(NotImplementedError):
        get_log_source({})
    store = SqliteLogStore(":memory:")
    common_log.set_store(store)
    try:
        assert isinstance(get_log_source({}), CommonLogSource)
    finally:
        common_log.set_store(None)
        store.close()


# ---------- A2A 단독 호출 ----------


def _skill(skill_id: str, allowed: list[str]) -> dict:
    return {
        "id": skill_id,
        "description": f"{skill_id} 테스트 skill. 상태를 바꾸지 않는다.",
        "input_schema": "contracts/src/ax_contracts/schemas/data.py#X",
        "output_schema": "contracts/src/ax_contracts/schemas/data.py#Y",
        "grade": "read",
        "allowed_requesters": allowed,
        "verifier": None,
        "priority": "recommended",
    }


CARD = AgentCard.model_validate(
    {
        "name": "data",
        "owner": "재현",
        "description": "데이터분석 테스트 카드",
        "url": "http://data:8106/a2a",
        "skills": [
            _skill("anomaly_detection", ["orchestrator", "infra"]),
            _skill("impact_analysis", ["orchestrator", "finance"]),
            _skill("fraud_score", ["orchestrator", "finance"]),
            _skill("kpi_summary", ["orchestrator", "dashboard-api"]),  # 실제 카드와 같다
        ],
    }
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("DATA_MOCK", "1")
    monkeypatch.setattr(handlers, "get_llm", lambda: MockLLMClient())
    return TestClient(create_agent_app(CARD, HANDLERS))


def _body(token: str, input: dict | None = None) -> dict:
    return {
        "meta": {
            "context_id": "ctx_req_0001",
            "token": token,
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "kpi_summary",
        "input": input or {},
    }


def test_a2a_dashboard_gets_kpi(client: TestClient) -> None:
    result = client.post("/a2a", json=_body("mock:dashboard-api")).json()
    assert result["status"] == "completed"
    incidents = result["output"]["incidents"]
    assert [i["mttr_seconds"] for i in incidents] == [540, 360]
    assert [i["used_memory"] for i in incidents] == [False, True]
    assert result["output"]["escalation_count"] == 1
    assert result["evidence"]["queries"]
    assert result["evidence"]["data_summary"].startswith(EXPECTED_SENTENCE)
    assert result["fallback"] is True


def test_a2a_support_is_forbidden(client: TestClient) -> None:
    result = client.post("/a2a", json=_body("mock:support")).json()
    assert result["status"] == "rejected"
    assert result["outcome"] == "FORBIDDEN_SKILL"


def test_a2a_reversed_period_is_invalid(client: TestClient) -> None:
    period = {"period_from": "2026-10-22T00:00:00Z", "period_to": "2026-10-21T00:00:00Z"}
    result = client.post("/a2a", json=_body("mock:orchestrator", period)).json()
    assert result["outcome"] == "invalid_input"


def test_a2a_without_store_is_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATA_MOCK")
    monkeypatch.delenv(common_log.DB_ENV, raising=False)
    common_log.set_store(None)
    result = client.post("/a2a", json=_body("mock:dashboard-api")).json()
    assert result["status"] == "failed"
    assert result["outcome"] == "unavailable"
