from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import AgentRequest, AgentResult, TaskStatus
from support_agent import handlers, notices
from support_agent.main import app
from support_agent.store import InquiryStore

NOTICE_VIEW = {
    "found": True,
    "notice_id": "ntc_0001",
    "status": "published",
    "channel_code": "banner",
    "public_text": "결제 장애가 복구되었습니다.",
    "promises": [
        {
            "type": "points",
            "amount": 1000,
            "condition_code": "paid_in_window",
            "condition": "장애 시간대 결제 고객",
        }
    ],
    "valid_to": "2026-10-30T00:00:00Z",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> list[AgentRequest]:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    monkeypatch.setattr(handlers, "STORE", InquiryStore())
    sent: list[AgentRequest] = []
    state: dict[str, Any] = {"view": NOTICE_VIEW, "status": TaskStatus.COMPLETED}

    async def fake_send(target: str, req: AgentRequest) -> AgentResult:
        assert target == "marketing" and req.skill == "get_notice"
        sent.append(req)
        return AgentResult(meta=req.meta, status=state["status"], output=state["view"])

    monkeypatch.setattr(handlers, "SEND", fake_send)
    sent_state = state
    _fresh.state = sent_state  # type: ignore[attr-defined]
    return sent


def create(notice_id: str | None = "ntc_0001") -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": "ctx_inq_0001",
            "token": "mock:dashboard-api",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": "k-1",
        },
        "skill": "create_inquiry",
        "input": {"user_id": 1, "customer_name": "김하늘", "notice_id": notice_id},
        "untrusted": {"subject": "공지 문의", "message": "공지에 나온 포인트는 언제 받나요?"},
    }
    return TestClient(app).post("/a2a", json=body).json()


def test_notice_becomes_first_source() -> None:
    result = create()
    sources = result["output"]["messages"][0]["analysis"]["sources"]
    assert sources[0]["id"] == "notice:ntc_0001"
    assert "1000원" in sources[0]["text"]
    assert "결제 장애가 복구" in sources[0]["text"]
    drafts = result["output"]["messages"][0]["analysis"]["drafts"]
    assert "notice:ntc_0001" in drafts[0]["source_ids"]


def test_get_notice_request_keeps_context_and_drops_idempotency_key(
    _fresh: list[AgentRequest],
) -> None:
    create()
    assert _fresh[0].input == {"notice_id": "ntc_0001"}
    assert _fresh[0].meta.context_id == "ctx_inq_0001"
    assert _fresh[0].meta.idempotency_key is None


def test_no_notice_id_does_not_call_marketing(_fresh: list[AgentRequest]) -> None:
    result = create(notice_id=None)
    assert _fresh == []
    sources = result["output"]["messages"][0]["analysis"]["sources"]
    assert all(s["doc_id"] != "notice" for s in sources)


def test_unknown_notice_gives_no_source() -> None:
    _fresh.state["view"] = {"found": False, "unknown_reason": "없음"}  # type: ignore[attr-defined]
    sources = create()["output"]["messages"][0]["analysis"]["sources"]
    assert all(s["doc_id"] != "notice" for s in sources)


def test_marketing_failure_gives_no_source() -> None:
    _fresh.state["status"] = TaskStatus.FAILED  # type: ignore[attr-defined]
    result = create()
    assert result["status"] == "completed"
    assert all(
        s["doc_id"] != "notice" for s in result["output"]["messages"][0]["analysis"]["sources"]
    )


def test_to_source_marks_ended_notice() -> None:
    source = notices.to_source({**NOTICE_VIEW, "status": "ended"})
    assert source is not None and "종료된 공지" in source.text
    assert notices.to_source({"found": False}) is None
