"""`receive_change_result`: 기획개발이 변경 요청을 닫으면 연결된 모든 문의에 결과를 적용한다."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_agent import handlers
from support_agent.change_requests import ChangeRequestStore
from support_agent.main import app, load_card
from support_agent.persistence import PersistentChangeRequestStore

CTX = "ctx_req_0001"
DONE = {
    "request_id": "REQ-12",
    "status": "completed",
    "pr_numbers": [52],
    "merged_at": "2026-10-23T09:30:00Z",
    "summary": "PR #52 머지로 완료",
}
DECLINED = {
    "request_id": "REQ-12",
    "status": "declined",
    "reason_code": "out_of_scope",
    "summary": "범위 밖이라 거절",
}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> ChangeRequestStore:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    changes = ChangeRequestStore()
    monkeypatch.setattr(handlers, "CHANGES", changes)
    return changes


def call(
    skill: str,
    input: dict[str, Any],
    context: str = CTX,
    requester: str = "planning",
) -> dict[str, Any]:
    body = {
        "meta": {
            "context_id": context,
            "token": f"mock:{requester}",
            "path": [],  # 결과 통지는 path를 비운 새 요청이다
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": f"{context}:change_result:REQ-12",
        },
        "skill": skill,
        "input": input,
    }
    return TestClient(app).post("/a2a", json=body).json()


def link_two(store: ChangeRequestStore) -> None:
    store.link(CTX, 7, request_id="REQ-12")
    store.link(CTX, 8, request_id="REQ-12", duplicate_of="REQ-12")
    store.link("ctx_req_0002", 9, request_id="REQ-30")  # 다른 사건


def test_card_declares_the_skill_for_planning_only() -> None:
    skill = {s.id: s for s in load_card().skills}["receive_change_result"]
    assert skill.grade == "A" and skill.allowed_requesters == ["planning"]
    assert call("receive_change_result", DONE, requester="dashboard-api")["status"] == "rejected"


def test_a_completed_result_is_applied_to_every_linked_inquiry(store: ChangeRequestStore) -> None:
    link_two(store)
    result = call("receive_change_result", DONE)
    assert result["status"] == "completed" and result["outcome"] == "applied"
    assert result["output"] == {"applied": 2}
    for inquiry_id in (7, 8):
        link = store.get(CTX, inquiry_id)
        assert link is not None and link.status == "completed"
        assert link.result["pr_numbers"] == [52] and link.result["summary"] == "PR #52 머지로 완료"
    other = store.get("ctx_req_0002", 9)
    assert other is not None and other.status == "requested"  # 다른 사건은 그대로


def test_the_result_is_visible_through_list_change_requests(store: ChangeRequestStore) -> None:
    link_two(store)
    call("receive_change_result", DONE)
    items = call("list_change_requests", {"status": "completed"}, requester="dashboard-api")[
        "output"
    ]["items"]
    assert sorted(i["inquiry_id"] for i in items) == [7, 8]
    assert items[0]["result"]["merged_at"].startswith("2026-10-23T09:30:00")


def test_a_declined_result_keeps_the_reason_code(store: ChangeRequestStore) -> None:
    link_two(store)
    result = call("receive_change_result", DECLINED)
    assert result["output"] == {"applied": 2}
    link = store.get(CTX, 7)
    assert link is not None and link.status == "declined"
    assert link.result["reason_code"] == "out_of_scope" and link.result["pr_numbers"] == []


def test_a_decline_without_a_reason_code_is_accepted_as_the_contract_allows(
    store: ChangeRequestStore,
) -> None:
    link_two(store)
    result = call("receive_change_result", {**DECLINED, "reason_code": None})
    assert result["output"] == {"applied": 2}
    link = store.get(CTX, 7)
    assert link is not None and link.status == "declined" and link.result["reason_code"] is None


def test_the_same_result_twice_is_applied_once(store: ChangeRequestStore) -> None:
    link_two(store)
    first = call("receive_change_result", DONE)
    before = [store.get(CTX, n).updated_at for n in (7, 8)]  # type: ignore[union-attr]
    again = call("receive_change_result", DONE)
    assert first["outcome"] == "applied" and again["outcome"] == "duplicate"
    assert again["output"] == {"applied": 2}
    assert [store.get(CTX, n).updated_at for n in (7, 8)] == before  # type: ignore[union-attr]


def test_a_different_result_after_closing_is_refused(store: ChangeRequestStore) -> None:
    link_two(store)
    call("receive_change_result", DONE)
    result = call("receive_change_result", DECLINED)
    assert result["status"] == "completed" and result["outcome"] == "invalid_state"
    assert "applied" not in result["output"]
    link = store.get(CTX, 7)
    assert link is not None and link.status == "completed"  # 처음 결과가 그대로다


def test_a_result_for_a_context_without_our_inquiries_applies_to_zero(
    store: ChangeRequestStore,
) -> None:
    result = call("receive_change_result", DONE, context="ctx_req_0099")
    assert result["status"] == "completed" and result["outcome"] == "not_linked"
    assert result["output"] == {"applied": 0}


def test_results_only_come_on_change_request_contexts(store: ChangeRequestStore) -> None:
    for wrong in ("ctx_inc_0001", "ctx_ui_0001", "ctx_ntc_0001"):
        result = call("receive_change_result", DONE, context=wrong)
        assert result["status"] == "failed" and result["outcome"] == "invalid_input", wrong


@pytest.mark.parametrize(
    "bad",
    [
        {**DECLINED, "pr_numbers": [1]},  # 거절인데 PR
        {**DONE, "reason_code": "x"},  # 완료인데 사유
        {**DONE, "status": "merged"},  # 계약에 없는 상태
        {"request_id": "REQ-12", "status": "completed"},  # 요약 없음
    ],
)
def test_inputs_that_break_the_contract_are_rejected_and_change_nothing(
    store: ChangeRequestStore, bad: dict[str, Any]
) -> None:
    link_two(store)
    result = call("receive_change_result", bad)
    assert result["status"] == "failed" and result["outcome"] == "invalid_input"
    assert all(store.get(CTX, n).status == "requested" for n in (7, 8))  # type: ignore[union-attr]


def test_a_long_summary_is_trimmed_not_stored_whole(store: ChangeRequestStore) -> None:
    link_two(store)
    call("receive_change_result", {**DONE, "summary": "가" * 1000})
    link = store.get(CTX, 7)
    assert link is not None and len(link.result["summary"]) == 300


def test_an_inquiry_linked_after_closing_inherits_the_result(store: ChangeRequestStore) -> None:
    link_two(store)
    call("receive_change_result", DONE)
    late = store.link(CTX, 10, request_id="REQ-12")  # 끝난 요청에 새 문의가 묶임
    assert late.status == "completed" and late.result["pr_numbers"] == [52]


def test_the_result_survives_a_restart(memory_repo: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")
    repo = memory_repo()
    monkeypatch.setattr(handlers, "CHANGES", PersistentChangeRequestStore(repo))
    link_two(handlers.CHANGES)
    assert call("receive_change_result", DONE)["output"] == {"applied": 2}

    reborn = PersistentChangeRequestStore(repo)  # 재시작
    monkeypatch.setattr(handlers, "CHANGES", reborn)
    link = reborn.get(CTX, 7)
    assert link is not None and link.status == "completed" and link.result["pr_numbers"] == [52]
    assert call("receive_change_result", DONE)["outcome"] == "duplicate"  # 재전송도 한 번만
