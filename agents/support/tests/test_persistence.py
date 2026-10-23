"""영속 저장소 테스트.

DB 없이 도는 테스트는 `MemoryRepo`(저장소 인터페이스의 메모리 구현)로 "저장 → 다시 시작 → 이어짐"을
확인한다. `SUPPORT_PG_DSN`이 있을 때만 도는 테스트는 실제 PostgreSQL(`PgRepo`)로 같은 것을 확인한다.
"""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from support_agent import claim_handlers, compensation, handlers, persistence
from support_agent.claims import ClaimStore
from support_agent.main import app
from support_agent.persistence import (
    BY_NAME,
    TABLES,
    PersistentClaimStore,
    PersistentInquiryStore,
    PersistentSequenceLedger,
    PgRepo,
    schema_sql,
)
from support_agent.store import InquiryStore


class MemoryRepo:
    """`PgRepo`와 같은 인터페이스. JSON으로 직렬화해 저장해서 DB와 같은 제약(JSON만 가능)을 둔다."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple, str]] = {t.name: {} for t in TABLES}
        self.fail = False

    def load(self, table: str) -> list[dict[str, Any]]:
        rows = self.tables[table]
        return [json.loads(rows[key]) for key in sorted(rows)]

    def upsert(self, table: str, values: dict[str, Any], doc: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("db down")
        spec = BY_NAME[table]
        assert set(values) == {*spec.pk, *spec.columns}  # 컬럼을 빠뜨리지 않는다
        self.tables[table][tuple(values[c] for c in spec.pk)] = json.dumps(doc, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.delenv(persistence.DSN_ENV, raising=False)


def call(
    client: TestClient,
    skill: str,
    input: dict[str, Any] | None = None,
    untrusted: dict[str, Any] | None = None,
    key: str | None = None,
    context: str = "ctx_req_0001",
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": context,
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input or {},
        "untrusted": untrusted or {},
    }
    return client.post("/a2a", json=body).json()


def boot(monkeypatch: pytest.MonkeyPatch, repo: Any) -> None:
    """ "재시작": 같은 저장소(DB)에서 모든 저장소를 새로 만들어 핸들러에 끼운다."""
    monkeypatch.setattr(handlers, "STORE", PersistentInquiryStore(repo))
    monkeypatch.setattr(handlers, "LEDGER", PersistentSequenceLedger(repo))
    monkeypatch.setattr(claim_handlers, "CLAIMS", PersistentClaimStore(repo))


# ---------------------------------------------------------------- 스키마·선택
def test_every_table_is_plural_snake_case_with_tenant_and_context() -> None:
    sql = schema_sql()
    for table in TABLES:
        assert re.fullmatch(r"[a-z]+(_[a-z]+)*s", table.name), table.name  # 복수형 snake_case
        columns = {*table.pk, *table.columns}
        assert {"tenant_id", "context_id"} <= columns, table.name
        assert f"CREATE TABLE IF NOT EXISTS {table.name}" in sql
    assert sql.count("doc jsonb NOT NULL") == len(TABLES)


def test_without_dsn_stores_are_plain_memory() -> None:
    assert persistence.dsn() is None
    assert type(persistence.build_inquiry_store()) is InquiryStore
    assert type(persistence.build_claim_store()) is ClaimStore
    assert type(persistence.build_sequence_ledger()) is compensation.SequenceLedger


# ---------------------------------------------------------------- 문의
def create_inquiry(client: TestClient, key: str = "k-1") -> dict[str, Any]:
    return call(
        client,
        "create_inquiry",
        {"user_id": 1, "customer_name": "김하늘", "order_id": 101},
        {"subject": "배송 문의", "message": "배송이 늦어요"},
        key=key,
    )


def test_inquiries_survive_restart_and_ids_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MemoryRepo()
    boot(monkeypatch, repo)
    client = TestClient(app)
    first = create_inquiry(client)["output"]
    inquiry_id = first["id"]
    call(client, "add_customer_message", {"inquiry_id": inquiry_id}, {"message": "아직이요"}, "k-2")
    call(client, "send_reply", {"inquiry_id": inquiry_id}, {"text": "확인 중입니다"}, "k-3")
    call(client, "set_inquiry_status", {"inquiry_id": inquiry_id, "status": "resolved"}, {}, "k-4")
    before = call(client, "get_inquiry", {"inquiry_id": inquiry_id})["output"]

    boot(monkeypatch, repo)  # 재시작
    after = call(client, "get_inquiry", {"inquiry_id": inquiry_id})["output"]
    assert after == before
    assert [m["role"] for m in after["messages"]] == ["customer", "customer", "agent"]
    assert after["status"] == "resolved"

    nxt = create_inquiry(client, key="k-5")["output"]
    assert nxt["id"] == inquiry_id + 1
    assert nxt["messages"][0]["id"] > max(m["id"] for m in after["messages"])  # 메시지 ID도 이어짐


def test_idempotent_replay_works_across_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MemoryRepo()
    boot(monkeypatch, repo)
    client = TestClient(app)
    first = create_inquiry(client, key="same")["output"]
    boot(monkeypatch, repo)
    second = create_inquiry(client, key="same")["output"]
    assert second == first
    assert len(call(client, "list_inquiries")["output"]["items"]) == 1  # 다시 만들지 않는다


def test_draft_events_are_saved_with_context_and_feed_kpis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MemoryRepo()
    boot(monkeypatch, repo)
    client = TestClient(app)
    inquiry_id = create_inquiry(client)["output"]["id"]
    call(
        client,
        "send_reply",
        {"inquiry_id": inquiry_id, "draft_style": "concise", "draft_text": "확인 중입니다"},
        {"text": "확인 중입니다"},
        "k-r",
    )
    call(client, "reject_draft", {"inquiry_id": inquiry_id, "draft_style": "empathetic"}, {}, "k-d")
    assert {e["result"] for e in repo.load("draft_events")} == {"accepted", "rejected"}
    assert all(e["context_id"] == "ctx_req_0001" for e in repo.load("draft_events"))

    boot(monkeypatch, repo)
    kpis = call(client, "inquiry_overview")["output"]["kpis"]
    assert (kpis["accepted_as_is"], kpis["rejected"]) == (1, 1)  # 재시작 후에도 집계가 이어진다


# ---------------------------------------------------------------- 클레임
def create_claim(client: TestClient, key: str = "c-1", **fields: Any) -> dict[str, Any]:
    payload = {
        "user_id": 1,
        "customer_name": "김하늘",
        "order_id": 101,
        "type": "return",
        "reason": "defect",
        "evidence_count": 2,
        **fields,
    }
    return call(client, "create_claim", payload, {"description": "찢어졌어요"}, key=key)


def test_claim_lifecycle_survives_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MemoryRepo()
    boot(monkeypatch, repo)
    client = TestClient(app)
    claim_id = create_claim(client)["output"]["id"]
    call(client, "decide_claim", {"claim_id": claim_id, "decision": "approve"}, {}, "c-2")
    call(client, "receive_return", {"claim_id": claim_id}, {}, "c-3")
    call(client, "inspect_return", {"claim_id": claim_id, "inspection": "pass"}, {}, "c-4")
    before = call(client, "get_claim", {"claim_id": claim_id})["output"]
    assert before["decided_by"] and before["inspection"] == "pass"
    assert [a["action"] for a in before["audit"]] == ["decide", "approve", "receive", "inspection"]

    boot(monkeypatch, repo)  # 재시작 — 큐도 DB 기록에서 다시 만들어진다
    assert call(client, "get_claim", {"claim_id": claim_id})["output"] == before
    queue = call(client, "claim_queue")["output"]["queue"]
    assert [e["claim_id"] for e in queue] == [claim_id] and queue[0]["ready"] is True

    run = call(client, "process_claim_queue", {}, key="c-5")["output"]
    assert run["paid"] == 1
    boot(monkeypatch, repo)  # 지급 후 다시 시작해도 완료 상태와 지급 기록이 남는다
    final = call(client, "get_claim", {"claim_id": claim_id})["output"]
    assert final["status"] == "completed" and final["refund_paid"] == 33000
    assert [a["action"] for a in final["audit"]][-1] == "pay"
    assert call(client, "claim_queue")["output"]["queue"] == []


def test_claim_ids_continue_and_duplicates_are_still_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MemoryRepo()
    boot(monkeypatch, repo)
    client = TestClient(app)
    first = create_claim(client)["output"]["id"]
    boot(monkeypatch, repo)
    assert create_claim(client, key="c-2")["outcome"] == "duplicate_claim"  # 진행 중 클레임을 기억
    other = create_claim(client, key="c-3", order_id=102, user_id=2, type="cancel", reason="other")
    assert other["output"]["id"] == first + 1


def test_claim_idempotent_replay_works_across_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MemoryRepo()
    boot(monkeypatch, repo)
    client = TestClient(app)
    first = create_claim(client, key="same")["output"]
    boot(monkeypatch, repo)
    assert create_claim(client, key="same")["output"] == first


# ---------------------------------------------------------------- 보상 sequence
def test_compensation_sequence_and_results_continue_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = MemoryRepo()
    boot(monkeypatch, repo)
    client = TestClient(app)
    inquiry_id = create_inquiry(client)["output"]["id"]
    sent: list[AgentRequest] = []

    async def marketing(target: str, req: AgentRequest) -> AgentResult:
        sent.append(req)
        return AgentResult(
            meta=req.meta, status=TaskStatus.COMPLETED, output={"coupon_id": f"cpn_{len(sent)}"}
        )

    monkeypatch.setattr(handlers, "SEND", marketing)

    def request_compensation(key: str, buyer: str) -> dict[str, Any]:
        return call(
            client,
            "request_compensation",
            {"inquiry_id": inquiry_id, "buyer_id": buyer, "reason_code": "late_delivery"},
            key=key,
        )

    first = request_compensation("comp-1", "usr_1")["output"]
    assert first["sequence"] == 1

    boot(
        monkeypatch, repo
    )  # 재시작해도 번호가 1로 돌아가지 않는다 (마케팅은 SEQUENCE_GAP으로 막는다)
    second = request_compensation("comp-2", "usr_2")["output"]
    assert second["sequence"] == 2
    again = request_compensation("comp-1", "usr_1")["output"]  # 같은 키는 이전 결과, 재전송 없음
    assert again == first and [r.sequence for r in sent] == [1, 2]


# ---------------------------------------------------------------- 쓰기 실패
def test_write_failure_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = MemoryRepo()
    store = PersistentInquiryStore(repo)
    repo.fail = True
    with pytest.raises(RuntimeError, match="db down"):
        store.results["k"] = {"x": 1}
    assert "k" not in store.results  # 저장에 실패한 결과는 캐시에도 넣지 않는다


# ---------------------------------------------------------------- 실제 PostgreSQL (DSN 있을 때만)
DSN = os.environ.get(persistence.DSN_ENV)
needs_db = pytest.mark.skipif(not DSN, reason="SUPPORT_PG_DSN 없음 (DB 테스트 건너뜀)")


@pytest.fixture
def pg_repo():
    assert DSN
    repo = PgRepo(DSN)
    for table in TABLES:
        repo._run(lambda conn, t=table.name: conn.execute(f"TRUNCATE {t}"))
    yield repo
    repo.close()


@needs_db
def test_pg_schema_is_idempotent_and_upsert_overwrites(pg_repo: PgRepo) -> None:
    PgRepo(DSN).close()  # 스키마를 다시 만들어도 오류가 없다
    values = {
        "tenant_id": "shop-demo",
        "idempotency_key": "ctx_req_1:a:b",
        "context_id": "ctx_req_1",
    }
    pg_repo.upsert("inquiry_results", values, {"key": "ctx_req_1:a:b", "result": {"n": 1}})
    pg_repo.upsert("inquiry_results", values, {"key": "ctx_req_1:a:b", "result": {"n": 2}})
    assert pg_repo.load("inquiry_results") == [{"key": "ctx_req_1:a:b", "result": {"n": 2}}]


@needs_db
def test_pg_reconnects_after_the_connection_is_lost(pg_repo: PgRepo) -> None:
    values = {
        "tenant_id": "shop-demo",
        "idempotency_key": "ctx_req_1:a:b",
        "context_id": "ctx_req_1",
    }
    pg_repo.upsert("inquiry_results", values, {"key": "ctx_req_1:a:b", "result": {"n": 1}})
    pg_repo._conn.close()  # 연결이 끊긴 상황
    pg_repo.upsert("inquiry_results", values, {"key": "ctx_req_1:a:b", "result": {"n": 3}})
    assert pg_repo.load("inquiry_results")[0]["result"] == {"n": 3}


@needs_db
def test_pg_full_flow_survives_restart(pg_repo: PgRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    boot(monkeypatch, pg_repo)
    client = TestClient(app)
    inquiry = create_inquiry(client)["output"]
    claim = create_claim(client)["output"]
    call(client, "decide_claim", {"claim_id": claim["id"], "decision": "approve"}, {}, "c-2")
    before = call(client, "get_claim", {"claim_id": claim["id"]})["output"]

    boot(monkeypatch, PgRepo(DSN))  # 새 연결 = 프로세스 재시작
    assert call(client, "get_claim", {"claim_id": claim["id"]})["output"] == before
    assert call(client, "get_inquiry", {"inquiry_id": inquiry["id"]})["output"]["messages"]
    assert create_inquiry(client, key="k-1")["output"]["id"] == inquiry["id"]  # 멱등 결과도 DB에서
    assert (
        create_claim(client, key="c-9", order_id=102, user_id=2, type="cancel", reason="other")[
            "output"
        ]["id"]
        == claim["id"] + 1
    )
