"""A2A skill 3개 시나리오 (FR-AG-002~004, IF-INT-A2A-001~003)."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from ax_contracts.schemas.planning import (
    FindRelatedPrsOutput,
    LinkIncidentOutput,
    VerifyRollbackTargetOutput,
)
from planning_agent.main import app


def _call(body: dict) -> dict:
    return TestClient(app).post("/a2a", json=body).json()


def _add_auto_prs(service, n: int = 2) -> None:
    """간단 요청을 n건 보내 자동 머지 PR을 만든다 (10-22 KST, shop-web/ 경로)."""
    for _ in range(n):
        asyncio.run(service.inbound("support_simple"))


# ---- find_related_prs


def test_find_related_prs_returns_only_prs_in_window(service, a2a_body) -> None:
    _add_auto_prs(service)  # 목 PR 3건 (PR #28 + 자동 2건), 구간 안은 PR #28 하나
    window = {"window_from": "2026-10-21T04:50:00Z", "window_to": "2026-10-21T05:10:00Z"}
    result = _call(a2a_body("find_related_prs", window, token="mock:data"))
    assert result["status"] == "completed"
    out = FindRelatedPrsOutput.model_validate(result["output"])
    assert [c.pr_number for c in out.candidates] == [28]
    pr = out.candidates[0]
    assert (pr.spec_id, pr.para_id) == ("SPEC-5", "R3")
    assert pr.changed_paths == ["shop-api/payment/config.py"]
    assert pr.merged_at.isoformat() == "2026-10-21T05:02:00+00:00"
    assert "pr:28" in result["evidence"]["ref_ids"]
    assert "para:SPEC-5:R3" in result["evidence"]["ref_ids"]
    assert "원인 판정은 하지 않음" in result["evidence"]["data_summary"]


def test_find_related_prs_orders_by_merged_at_desc_and_filters_component(service, a2a_body) -> None:
    _add_auto_prs(service)
    window = {"window_from": "2026-10-20T00:00:00Z", "window_to": "2026-10-23T00:00:00Z"}
    everything = FindRelatedPrsOutput.model_validate(
        _call(a2a_body("find_related_prs", window))["output"]
    )
    assert [c.pr_number for c in everything.candidates] == [32, 31, 28]
    times = [c.merged_at for c in everything.candidates]
    assert times == sorted(times, reverse=True)

    web = _call(a2a_body("find_related_prs", {**window, "component": "shop-web/"}))
    assert [c["pr_number"] for c in web["output"]["candidates"]] == [32, 31]
    api = _call(a2a_body("find_related_prs", {**window, "component": "shop-api/payment"}))
    assert [c["pr_number"] for c in api["output"]["candidates"]] == [28]


def test_find_related_prs_empty_window_says_unknown(a2a_body) -> None:
    window = {"window_from": "2026-10-01T00:00:00Z", "window_to": "2026-10-02T00:00:00Z"}
    result = _call(a2a_body("find_related_prs", window))
    assert result["status"] == "completed"
    assert result["output"] == {"candidates": []}
    assert result["evidence"]["data_summary"].startswith("확인할 수 없음")


def test_find_related_prs_invalid_input_fails_without_crash(a2a_body) -> None:
    result = _call(a2a_body("find_related_prs", {"window_from": "어제"}))
    assert (result["status"], result["outcome"]) == ("failed", "invalid_input")


def test_find_related_prs_logs_activity_and_reply_to_outbox(service, a2a_body) -> None:
    window = {"window_from": "2026-10-21T04:50:00Z", "window_to": "2026-10-21T05:10:00Z"}
    _call(a2a_body("find_related_prs", window, token="mock:data"))
    last = service.state.activity[-2:]
    assert [(a.from_, a.to, a.point) for a in last] == [
        ("data", "planning", "find_related_prs"),
        ("planning", "data", "find_related_prs"),
    ]
    assert "원인 판정은 하지 않음" in last[1].text
    assert service.state.outbox[-1].to == "data"


# ---- link_incident


def test_link_incident_marks_para_and_registers_follow_up(service, a2a_body) -> None:
    payload = {
        "incident_context_id": "ctx_inc_0002",
        "pr_numbers": [28],
        "follow_up_title": "결제 설정 변경은 사전 부하 테스트 필수",
    }
    result = _call(
        a2a_body(
            "link_incident",
            payload,
            token="mock:orchestrator",
            context_id="ctx_inc_0002",
            idempotency_key="ctx_inc_0002:link_incident:28",
        )
    )
    assert (result["status"], result["outcome"]) == ("completed", None)
    out = LinkIncidentOutput.model_validate(result["output"])
    assert out.linked_paras == ["SPEC-5:R3"]
    follow = next(r for r in service.state.requests if r.id == out.follow_up_request_id)
    assert (follow.title, follow.state, follow.context_id) == (
        "결제 설정 변경은 사전 부하 테스트 필수",
        "접수",
        "ctx_inc_0002",
    )
    assert follow.id == "REQ-102"
    spec5 = next(s for s in service.state.specs if s.id == "SPEC-5")
    assert next(p for p in spec5.paras if p.id == "R3").incident == "ctx_inc_0002"
    incident = service.state.incidents[0]
    assert (incident.context_id, incident.follow_up, incident.linked) == (
        "ctx_inc_0002",
        "REQ-102",
        True,
    )
    assert [c.pr for c in incident.candidates] == ["PR #28"]
    assert "link_incident" in [c.action for c in service.gate_calls]


def test_link_incident_same_follow_up_title_is_linked_duplicate(service, a2a_body) -> None:
    before = len(service.state.requests)
    payload = {
        "incident_context_id": "ctx_inc_0001",
        "pr_numbers": [28],
        "follow_up_title": "결제 경로 설정 변경은 카나리 배포 필수",  # REQ-101과 같은 제목
    }
    result = _call(
        a2a_body("link_incident", payload, idempotency_key="ctx_inc_0001:link_incident:28")
    )
    assert (result["status"], result["outcome"]) == ("completed", "linked_duplicate")
    assert result["output"]["follow_up_request_id"] == "REQ-101"
    assert len(service.state.requests) == before
    assert "create_ticket" not in [c.action for c in service.gate_calls]


def test_link_incident_on_auto_pr_shrinks_auto_scope(service, a2a_body) -> None:
    _add_auto_prs(service, 1)  # PR #31 (자동 머지)
    assert service.state.auto_policy.level == 1
    payload = {"incident_context_id": "ctx_inc_0003", "pr_numbers": [31]}
    result = _call(
        a2a_body("link_incident", payload, idempotency_key="ctx_inc_0003:link_incident:31")
    )
    assert result["status"] == "completed"
    assert result["output"]["follow_up_request_id"] is None
    pol = service.state.auto_policy
    assert pol.level == 0 and pol.streak == 0
    assert pol.history[-1].result == "장애 연결"
    assert pol.log[-1].by == "자동"
    assert service.state.activity[-1].point == "auto_policy.updated"


# ---- verify_rollback_target


def _verify(a2a_body, deployed_at: str, opened_at: str = "2026-10-21T05:04:00Z") -> dict:
    payload = {
        "service": "shop-api",
        "target_version": "v2.4.1",
        "deployed_at": deployed_at,
        "incident_opened_at": opened_at,
    }
    return _call(a2a_body("verify_rollback_target", payload, token="mock:safety"))


def test_verify_approves_deploy_before_incident_with_merged_pr(a2a_body) -> None:
    result = _verify(a2a_body, "2026-10-21T05:03:00Z")
    assert (result["status"], result["outcome"]) == ("completed", None)
    out = VerifyRollbackTargetOutput.model_validate(result["output"])
    assert (out.approved, out.reason_code) == (True, "deploy_before_incident")
    assert [p.pr_number for p in out.related_prs] == [28]
    assert out.related_prs[0].para_id == "R3"


def test_verify_rejects_deploy_after_incident(a2a_body) -> None:
    result = _verify(a2a_body, "2026-10-21T05:10:00Z")
    assert (result["status"], result["outcome"]) == ("completed", "rejected_by_review")
    assert result["output"] == {
        "approved": False,
        "reason_code": "deploy_after_incident",
        "related_prs": [],
    }


def test_verify_rejects_when_no_merge_in_window(a2a_body) -> None:
    result = _verify(a2a_body, "2026-10-20T05:03:00Z")
    assert (result["status"], result["outcome"]) == ("completed", "rejected_by_review")
    assert result["output"]["approved"] is False
    assert result["output"]["reason_code"] == "no_deploy_in_window"


# ---- verify_rollback_target: safety 교차검증 호출 형식 (`Gate._cross_verify`)

ROLLBACK_PARAMS = {
    "service": "shop-api",
    "from_version": "v2.4.1",
    "to_version": "v2.4.0",
    "release_fault_injection": True,
    "incident_opened_at": "2026-10-21T05:04:00Z",
}


def _rollback_review(
    a2a_body, deployed_at: str | None, action: str = "rollback_deployment", **extra
) -> dict:
    """safety `Gate._cross_verify`가 verifier skill에 넣는 input과 같은 모양."""
    params = {**ROLLBACK_PARAMS, **extra}
    if deployed_at is not None:
        params["deployed_at"] = deployed_at
    payload = {
        "execution_id": "exe_rb_1",
        "action": action,
        "requester": "infra",
        "params": params,
        "computed_amount": None,
        "threshold_version": "2026-10-23",
    }
    body = a2a_body(
        "verify_rollback_target", payload, token="mock:safety", idempotency_key="verify:exe_rb_1"
    )
    return _call(body)


def _safety_verdict(result: dict) -> str:
    """safety `call_verifier` 규칙: completed + (decision approved 또는 approved true)만 승인."""
    if result["status"] != "completed":
        return "unavailable"
    out = result["output"]
    approved = out.get("decision") == "approved" or out.get("approved") is True
    return "approved" if approved else "rejected"


def test_verify_review_format_approves(a2a_body) -> None:
    result = _rollback_review(a2a_body, "2026-10-21T05:03:00Z")
    assert (result["status"], result["outcome"]) == ("completed", None)
    out = VerifyRollbackTargetOutput.model_validate(result["output"])
    assert (out.approved, out.reason_code) == (True, "deploy_before_incident")
    assert [p.pr_number for p in out.related_prs] == [28]
    assert _safety_verdict(result) == "approved"
    assert "execution:exe_rb_1" in result["evidence"]["ref_ids"]
    # 계약 형식과 같은 판정이다
    contract = _verify(a2a_body, "2026-10-21T05:03:00Z")
    assert result["output"] == contract["output"]
    # 계약 이름(target_version)으로 와도 같다
    named = _rollback_review(a2a_body, "2026-10-21T05:03:00Z", target_version="v2.4.1")
    assert named["output"] == result["output"]


def test_verify_review_format_rejects(a2a_body) -> None:
    after = _rollback_review(a2a_body, "2026-10-21T05:10:00Z")
    assert (after["status"], after["outcome"]) == ("completed", "rejected_by_review")
    assert after["output"]["reason_code"] == "deploy_after_incident"
    assert _safety_verdict(after) == "rejected"
    none = _rollback_review(a2a_body, "2026-10-20T05:03:00Z")
    assert (none["status"], none["outcome"]) == ("completed", "rejected_by_review")
    assert none["output"]["reason_code"] == "no_deploy_in_window"
    assert _safety_verdict(none) == "rejected"


def test_verify_review_format_bad_input_fails(a2a_body) -> None:
    # 배포 시각이 없으면 판정하지 않는다 → failed (safety는 무응답 → 보류)
    no_deploy = _rollback_review(a2a_body, None)
    assert (no_deploy["status"], no_deploy["outcome"]) == ("failed", "invalid_input")
    assert _safety_verdict(no_deploy) == "unavailable"
    # 되돌릴 버전(from_version)도 없으면 실패
    no_version = _rollback_review(a2a_body, "2026-10-21T05:03:00Z", from_version=None)
    assert (no_version["status"], no_version["outcome"]) == ("failed", "invalid_input")
    # 다른 액션의 검토는 받지 않는다
    other = _rollback_review(a2a_body, "2026-10-21T05:03:00Z", action="restart_service")
    assert (other["status"], other["outcome"]) == ("failed", "invalid_input")
    assert _safety_verdict(other) == "unavailable"
