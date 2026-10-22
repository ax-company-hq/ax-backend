"""발령·퇴직·입사: 요청(A) → 재배치 확정(B, 코드 규칙) → 퇴직 확정(C, 보류)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import (
    ConfirmOffboardingOutput,
    ConfirmReassignmentOutput,
    RequestAssignmentChangeOutput,
)
from hr_agent.assignment import rules
from hr_agent.main import app
from hr_agent.roster import store as roster
from hr_agent.roster.gaps import compute_gaps


def _body(skill: str, inp: dict, key: str, ctx: str = "ctx_asg_0001") -> dict:
    return {
        "meta": {
            "context_id": ctx,
            "token": "mock:orchestrator",
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "idempotency_key": f"{ctx}:{key}",
        },
        "skill": skill,
        "input": inp,
    }


@pytest.fixture
def client(svc) -> TestClient:
    return TestClient(app)


def _request(client, kind, member, area, successor=None, ctx="ctx_asg_0001") -> dict:
    inp = {"change_kind": kind, "member_id": member, "area": area}
    if successor:
        inp["successor_id"] = successor
    return client.post("/a2a", json=_body("request_assignment_change", inp, "req", ctx)).json()


def _confirm(client, change_id, successor, key="rea", ctx="ctx_asg_0001") -> dict:
    inp = {"change_id": change_id, "successor_id": successor}
    return client.post("/a2a", json=_body("confirm_reassignment", inp, key, ctx)).json()


def _owner(svc, area) -> list[str]:
    return roster.owners(svc.store, area)


def test_request_builds_checklist_candidates_access_and_a_handover(svc, client) -> None:
    res = _request(client, "transfer", "emp_01", "payment")
    assert res["status"] == "completed"
    out = RequestAssignmentChangeOutput.model_validate(res["output"])
    assert out.successor_candidates[0] == "emp_02"  # 같은 영역 예비 담당이 먼저
    assert "emp_01" not in out.successor_candidates
    assert out.access_list == [f"revoke:payment/{r}" for r in ("repo", "deploy", "oncall")]
    assert len(out.checklist) == 3 and out.handover_id and out.onboarding_id is None
    assert svc.store.one("SELECT 1 FROM handovers WHERE handover_id = ?", (out.handover_id,))


def test_join_starts_onboarding_and_grants(svc, client) -> None:
    out = RequestAssignmentChangeOutput.model_validate(
        _request(client, "join", "emp_10", "inventory")["output"]
    )
    assert out.onboarding_id and out.handover_id is None and out.successor_candidates == []
    assert out.access_list[0].startswith("grant:inventory")


def test_same_request_twice_is_one_change(svc, client) -> None:
    a, b = (
        _request(client, "transfer", "emp_01", "payment"),
        _request(client, "transfer", "emp_01", "payment"),
    )
    assert b["outcome"] == "duplicate" and a["output"]["change_id"] == b["output"]["change_id"]
    assert len(svc.store.all("SELECT * FROM assignment_changes")) == 1


def test_overlapping_change_on_the_same_area_is_locked(svc, client) -> None:
    _request(client, "transfer", "emp_01", "payment", ctx="ctx_asg_0001")
    other = _request(client, "shift", "emp_02", "payment", ctx="ctx_asg_0002")
    assert (other["status"], other["outcome"]) == ("failed", "LOCKED")


def test_transfer_confirm_moves_the_owner_without_a_gap(svc, client) -> None:
    change = _request(client, "transfer", "emp_01", "payment")["output"]["change_id"]
    res = _confirm(client, change, "emp_02")
    out = ConfirmReassignmentOutput.model_validate(res["output"])
    assert (out.status, out.gap_areas, out.duplicate_areas) == ("confirmed", [], [])
    assert _owner(svc, "payment") == ["emp_02"]
    assert [e.member_id for e in roster.entries(svc.store, "payment")] == ["emp_02"]
    assert ("confirm_reassignment", "executed") in svc.gate.calls
    # 잠금이 풀려서 같은 영역의 다음 변경을 받을 수 있다
    nxt = _request(client, "shift", "emp_02", "payment", ctx="ctx_asg_0002")
    assert nxt["status"] == "completed"


def test_shift_keeps_the_member_as_backup(svc, client) -> None:
    change = _request(client, "shift", "emp_03", "search")["output"]["change_id"]
    _confirm(client, change, "emp_04")
    roles = {(e.member_id, e.role) for e in roster.entries(svc.store, "search")}
    assert roles == {("emp_04", "owner"), ("emp_03", "backup")}


def test_join_into_an_empty_area_becomes_its_owner(svc, client) -> None:
    assert "inventory" in compute_gaps(svc.store, svc.clock()).no_owner_areas
    change = _request(client, "join", "emp_10", "inventory")["output"]["change_id"]
    res = _confirm(client, change, "emp_10")
    assert res["output"]["status"] == "confirmed"
    assert _owner(svc, "inventory") == ["emp_10"]
    assert "inventory" not in compute_gaps(svc.store, svc.clock()).no_owner_areas


def test_rules_reject_a_change_that_leaves_an_area_empty(svc, client) -> None:
    # catalog 담당 1명이 빠지는데 후임이 본인이라 아무도 이어받지 않는 계획은 공백으로 잡힌다
    ops = rules.plan_ops("transfer", "emp_05", "catalog", "emp_05", [("owner", "emp_05")])
    coverage, _ = rules.simulate(svc.store, ops)
    assert coverage.gap_areas == ["catalog"]


def test_unsafe_confirm_is_rejected_and_nothing_changes(svc, client) -> None:
    change = _request(client, "offboarding", "emp_05", "catalog")["output"]["change_id"]
    before = [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)]
    # 후임 emp_05 본인 → 입력 오류, 명부는 그대로
    bad = _confirm(client, change, "emp_05")
    assert bad["outcome"] == "INVALID_INPUT"
    assert [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)] == before


def test_offboarding_is_held_then_applied_after_approval(svc, client) -> None:
    change = _request(client, "offboarding", "emp_01", "payment", "emp_02")["output"]["change_id"]
    off = client.post(
        "/a2a", json=_body("confirm_offboarding", {"change_id": change}, "off")
    ).json()
    assert off["outcome"] == "REASSIGN_FIRST"  # 재배치 확정이 먼저다
    _confirm(client, change, "emp_02")
    held = client.post(
        "/a2a", json=_body("confirm_offboarding", {"change_id": change}, "off")
    ).json()
    out = ConfirmOffboardingOutput.model_validate(held["output"])
    assert (out.status, bool(out.hold_id)) == ("held", True)
    assert roster.known_member(svc.store, "emp_01")
    assert svc.store.one("SELECT active FROM members WHERE member_id = 'emp_01'")["active"] == 1
    svc.gate.decide_hold(out.hold_id, approved=True)
    done = client.post(
        "/a2a", json=_body("confirm_offboarding", {"change_id": change}, "off")
    ).json()
    assert done["output"]["status"] == "confirmed"
    assert svc.store.one("SELECT active FROM members WHERE member_id = 'emp_01'")["active"] == 0
    assert roster.areas_of(svc.store, "emp_01") == []
    assert "emp_01" not in rules.successor_candidates(svc.store, "emp_02", "payment")


def test_offboarding_rejected_by_person_changes_nothing(svc, client) -> None:
    change = _request(client, "offboarding", "emp_01", "payment", "emp_02")["output"]["change_id"]
    _confirm(client, change, "emp_02")
    held = client.post(
        "/a2a", json=_body("confirm_offboarding", {"change_id": change}, "off")
    ).json()
    svc.gate.decide_hold(held["output"]["hold_id"], approved=False)
    again = client.post(
        "/a2a", json=_body("confirm_offboarding", {"change_id": change}, "off")
    ).json()
    assert again["status"] == "completed" and again["output"].get("status") != "confirmed"
    assert svc.store.one("SELECT active FROM members WHERE member_id = 'emp_01'")["active"] == 1


def test_offboarding_is_blocked_if_it_would_empty_another_area(svc, client) -> None:
    # emp_08은 infra-ops 담당. 다른 영역(search)에서도 유일한 담당이 되어 있다고 가정한다.
    roster.set_active(svc.store, "inventory", "owner", "emp_08", True)
    change = _request(client, "offboarding", "emp_08", "infra-ops", "emp_09")["output"]["change_id"]
    _confirm(client, change, "emp_09")
    held = client.post(
        "/a2a", json=_body("confirm_offboarding", {"change_id": change}, "off")
    ).json()
    svc.gate.decide_hold(held["output"]["hold_id"], approved=True)
    res = client.post(
        "/a2a", json=_body("confirm_offboarding", {"change_id": change}, "off")
    ).json()
    assert (res["status"], res["outcome"]) == ("failed", "COVERAGE_GAP")
    assert svc.store.one("SELECT active FROM members WHERE member_id = 'emp_08'")["active"] == 1


def test_invalid_requests(svc, client) -> None:
    assert _request(client, "transfer", "emp_01", "zzz")["outcome"] == "UNKNOWN_AREA"
    assert _request(client, "transfer", "emp_99", "payment")["outcome"] == "UNKNOWN_MEMBER"
    assert _request(client, "transfer", "emp_03", "payment")["outcome"] == "NOT_IN_AREA"
    assert _request(client, "join", "emp_01", "payment")["outcome"] == "ALREADY_IN_AREA"
    assert _request(client, "transfer", "emp_01", "payment", "emp_01")["outcome"] == "INVALID_INPUT"
    unknown = client.post(
        "/a2a",
        json=_body("confirm_reassignment", {"change_id": "chg_x", "successor_id": "emp_02"}, "k"),
    ).json()
    assert unknown["outcome"] == "UNKNOWN_CHANGE"


def test_confirm_twice_is_idempotent(svc, client) -> None:
    change = _request(client, "transfer", "emp_01", "payment")["output"]["change_id"]
    first = _confirm(client, change, "emp_02")
    second = _confirm(client, change, "emp_02")
    assert first["output"] == second["output"] and second["outcome"] == "duplicate"
    assert _owner(svc, "payment") == ["emp_02"]


def test_verifier_rejects_a_plan_that_leaves_a_gap_and_nothing_changes(
    svc, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    change = _request(client, "transfer", "emp_01", "payment")["output"]["change_id"]
    before = [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)]
    # 잘못된 계획(담당만 빼고 이어받는 사람이 없음)을 가정해 B 검증이 막는지 본다
    monkeypatch.setattr(rules, "plan_ops", lambda *a, **k: [("payment", "owner", "emp_01", False)])
    res = _confirm(client, change, "emp_02")
    out = ConfirmReassignmentOutput.model_validate(res["output"])
    assert (out.status, out.gap_areas) == ("rejected", ["payment"])
    assert ("confirm_reassignment", "rejected") in svc.gate.calls
    assert [(e.area, e.role, e.member_id) for e in roster.entries(svc.store)] == before
    assert svc.store.one("SELECT status FROM assignment_changes")["status"] == "requested"
