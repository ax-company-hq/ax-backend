"""감사 기록에 원문·마스킹한 글·값의 해시가 새지 않는다는 불변식 (설계 #442, 유출 0건)."""

import asyncio
import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_agent_template import common_log
from ax_contracts.data_access.common_log import CommonLogQuery
from ax_contracts.schemas import AgentRequest
from support_agent import guard, handlers, secure_transfer
from support_agent.main import app
from support_agent.store import InquiryStore

SECRETS = [
    "010-1234-5678",
    "010-9999-8888",
    "kim.secret@example.com",
    "900101-1234567",
    "4111 1111 1111 1111",
    "123-456-789012",
    "김철수",
    "서울시 강남구 테헤란로 1",
]
MESSAGE = (
    "김철수입니다. 서울시 강남구 테헤란로 1 에서 주문했어요. 연락처 010-1234-5678, "
    "메일 kim.secret@example.com, 주민번호 900101-1234567, 카드 4111 1111 1111 1111, "
    "계좌번호: 123-456-789012. 환불해 주세요. 전화는 010-9999-8888 로도 받아요."
)
SENTENCES = ["환불해 주세요", "에서 주문했어요", "로도 받아요"]

MASKING_KEYS = {
    "audit_id", "record_type", "tenant_id", "context_id", "inquiry_id", "message_id", "seq",
    "stage", "detected", "action", "reason_code", "rules_version", "occurred_at",
}  # fmt: skip
PROVENANCE_KEYS = {
    "audit_id", "record_type", "tenant_id", "context_id", "inquiry_id", "message_id",
    "draft_style", "source_ids", "path", "model_name", "guard_codes", "outcome", "actor",
    "occurred_at",
}  # fmt: skip


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setenv("SUPPORT_LLM_MODEL", "test-model")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def call(
    client: TestClient,
    skill: str,
    input: dict[str, Any],
    untrusted: dict[str, Any] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_inq_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": key,
        },
        "skill": skill,
        "input": input,
        "untrusted": untrusted or {},
    }
    return client.post("/a2a", json=body).json()


def _leaks(dumped: str) -> list[str]:
    """덤프에 들어 있으면 안 되는 것: 원문 값, 문장, 마스킹한 글, 값의 해시."""
    forbidden = [*SECRETS, *SENTENCES, guard.mask_pii(MESSAGE)[0]]
    hashed = [
        getattr(hashlib, algo)(value.encode()).hexdigest()
        for value in SECRETS
        for algo in ("md5", "sha1", "sha256")
    ]
    return [needle for needle in (*forbidden, *hashed) if needle in dumped]


def _drive(client: TestClient) -> int:
    """감사 기록이 생기는 모든 경로를 개인정보가 든 입력으로 지나간다."""
    inquiry_id = call(
        client,
        "create_inquiry",
        {"user_id": 1, "customer_name": "김철수", "order_id": 101, "order_no": "O-1"},
        {"subject": "김철수 환불 문의", "message": MESSAGE},
        key="k-leak-1",
    )["output"]["id"]
    call(
        client,
        "add_customer_message",
        {"inquiry_id": inquiry_id},
        {"message": "다시 연락주세요 010-9999-8888 kim.secret@example.com"},
        key="k-leak-2",
    )
    call(
        client,
        "send_reply",
        {"inquiry_id": inquiry_id, "draft_style": "concise"},
        {"text": "010-1234-5678 로 연락드리겠습니다"},  # 답변 가드가 막는 경로
        key="k-leak-3",
    )
    call(
        client,
        "send_reply",
        {"inquiry_id": inquiry_id, "draft_style": "empathetic"},
        {"text": "김철수 고객님 확인 후 안내드리겠습니다"},
        key="k-leak-4",
    )
    call(
        client,
        "reject_draft",
        {"inquiry_id": inquiry_id, "draft_style": "step_by_step"},
        key="k-leak-5",
    )
    return inquiry_id


def test_stored_records_and_skill_output_never_contain_raw_values(client: TestClient) -> None:
    inquiry_id = _drive(client)
    records = handlers.STORE.privacy_audit
    assert records  # 빈 기록으로 통과하는 것을 막는다
    assert _leaks(json.dumps(records, ensure_ascii=False)) == []
    out = call(client, "get_privacy_audit", {"inquiry_id": inquiry_id})["output"]
    assert _leaks(json.dumps(out, ensure_ascii=False)) == []


def test_records_only_carry_allowlisted_fields_with_scalar_or_count_values(
    client: TestClient,
) -> None:
    _drive(client)
    for record in handlers.STORE.privacy_audit:
        allowed = MASKING_KEYS if record["record_type"] == "masking" else PROVENANCE_KEYS
        assert set(record) <= allowed, set(record) - allowed
        if record["record_type"] == "masking":
            assert set(record["detected"]) == set(guard_kinds())
            assert all(isinstance(n, int) and n >= 0 for n in record["detected"].values())


def guard_kinds() -> tuple[str, ...]:
    return ("phone", "email", "rrn", "card", "account")


def test_blocked_reply_records_guard_codes_not_the_blocked_text(client: TestClient) -> None:
    inquiry_id = _drive(client)
    blocked = [
        r
        for r in handlers.STORE.privacy_audit
        if r["record_type"] == "provenance" and r["outcome"] == "blocked"
    ]
    assert blocked and all(r["guard_codes"] for r in blocked)
    assert "pii_in_reply" in {c for r in blocked for c in r["guard_codes"]}
    assert "연락드리겠습니다" not in json.dumps(blocked, ensure_ascii=False)
    assert inquiry_id == blocked[0]["inquiry_id"]


def test_transfer_block_record_has_counts_but_not_the_request_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(secure_transfer.KEY_ENV, raising=False)

    async def raw(target: str, request: AgentRequest) -> Any:
        raise AssertionError("차단된 요청은 보내지지 않는다")

    monkeypatch.setattr(secure_transfer, "_raw_send", raw)
    request = AgentRequest.model_validate(
        {
            "meta": {
                "context_id": "ctx_inq_0001",
                "token": "mock:support",
                "path": [],
                "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            },
            "skill": "diagnose",
            "input": {"inquiry_id": 7, "note": MESSAGE},
            "untrusted": {},
        }
    )
    asyncio.run(secure_transfer.send("infra", request))
    record = handlers.STORE.privacy_audit[-1]
    assert sum(record["detected"].values()) >= 5
    assert _leaks(json.dumps(record, ensure_ascii=False)) == []


def _record_text(record: logging.LogRecord) -> str:
    """로그 한 줄이 사람에게 보여 줄 모든 글: 완성한 메시지, 인자, 예외, 덧붙인 필드."""
    standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
    extra = {k: v for k, v in record.__dict__.items() if k not in standard}
    parts = [record.getMessage(), repr(record.msg), repr(record.args), record.exc_text or ""]
    if record.exc_info:
        parts.append("".join(logging.Formatter().formatException(record.exc_info)))
    parts.append(repr(extra))
    return "\n".join(parts)


def test_no_log_line_or_console_output_carries_the_values(
    client: TestClient, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    # 필터 없이 모든 로거의 모든 줄을 본다. 감사 기록 때문에만 생기는 줄로 좁히면 놓친다
    with caplog.at_level(logging.DEBUG):
        _drive(client)
    assert any(r.name == "ax.event" for r in caplog.records)  # 검사할 줄이 실제로 있었다
    everything = "\n".join(_record_text(r) for r in caplog.records)
    assert _leaks(everything) == []
    console = capfd.readouterr()
    assert _leaks(console.out + console.err) == []


def test_leak_check_catches_a_planted_value(caplog: pytest.LogCaptureFixture) -> None:
    """위 검사가 항상 통과하는 빈 검사가 아님을 보인다: 일부러 새게 하면 잡아야 한다."""
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("ax.event").info("연락처 %s", "010-1234-5678")
        logging.getLogger("x").warning("메일", extra={"who": "kim.secret@example.com"})
    leaked = _leaks("\n".join(_record_text(r) for r in caplog.records))
    assert "010-1234-5678" in leaked and "kim.secret@example.com" in leaked


def test_common_log_rows_never_carry_the_values(client: TestClient, tmp_path: Path) -> None:
    """대시보드가 읽는 공통 로그 저장소의 모든 줄(모든 열)을 파일에서 직접 읽어 본다."""
    db = tmp_path / "common.db"
    common_log.set_store(common_log.SqliteLogStore(str(db)))
    try:
        _drive(client)
        store = common_log.get_store()
        assert store is not None
        assert store.query(CommonLogQuery(kinds=["event_published"])).records  # 줄이 실제로 있다
    finally:
        common_log.reset_store()
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT * FROM common_logs").fetchall()
    assert len(rows) >= 2
    assert _leaks(json.dumps(rows, ensure_ascii=False, default=str)) == []
