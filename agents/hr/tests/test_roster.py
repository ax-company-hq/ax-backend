"""명부 조회와 공백 점검."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ax_contracts.schemas import GetRosterOutput
from hr_agent.main import app
from hr_agent.roster import store as roster
from hr_agent.roster.gaps import compute_gaps


def _body(area: str | None = None, token: str = "mock:planning") -> dict:
    return {
        "meta": {
            "context_id": "ctx_ros_0001",
            "token": token,
            "path": [],
            "deadline": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        },
        "skill": "get_roster",
        "input": {"area": area} if area else {},
    }


@pytest.fixture
def client(svc) -> TestClient:
    return TestClient(app)


def test_roster_lists_entries_with_shift_and_gap_areas(svc, client) -> None:
    res = client.post("/a2a", json=_body()).json()
    assert res["status"] == "completed"
    out = GetRosterOutput.model_validate(res["output"])
    assert {e.area for e in out.entries} >= {"payment", "search", "infra-ops"}
    payment = next(e for e in out.entries if e.area == "payment" and e.role == "owner")
    assert payment.member_id == "emp_01" and payment.shift_start and payment.shift_end
    assert "inventory" in out.gap_areas  # 담당 없는 영역
    assert out.collected_at == svc.clock()


def test_area_filter_and_unknown_area(svc, client) -> None:
    out = GetRosterOutput.model_validate(client.post("/a2a", json=_body("search")).json()["output"])
    assert {e.area for e in out.entries} == {"search"}
    bad = client.post("/a2a", json=_body("nowhere")).json()
    assert (bad["status"], bad["outcome"]) == ("rejected", "INVALID_INPUT")


def test_roster_has_no_personal_fields(svc, client) -> None:
    entry = client.post("/a2a", json=_body()).json()["output"]["entries"][0]
    assert set(entry) == {"area", "role", "member_id", "shift_start", "shift_end"}


def test_shift_gap_appears_when_nobody_takes_over(svc) -> None:
    # catalog는 담당 1명이 9~18시만 일한다 → 18시에 비는 시각이 잡힌다
    gaps = compute_gaps(svc.store, svc.clock())
    assert "inventory" in gaps.no_owner_areas
    assert any(area == "catalog" for area, _ in gaps.shift_gaps)
    assert "payment" not in gaps.gap_areas  # 두 명이 이어받는다


def test_removing_the_owner_makes_the_area_a_gap(svc) -> None:
    roster.set_active(svc.store, "search", "owner", "emp_03", False)
    # 예비 담당이 남아 있어도 담당(owner)이 없으면 공백이다
    assert "search" in compute_gaps(svc.store, svc.clock()).no_owner_areas
    assert [e.member_id for e in roster.entries(svc.store, "search")] == ["emp_04"]
    assert roster.areas_of(svc.store, "emp_03") == []


def test_roster_is_deterministic(svc, client) -> None:
    first = client.post("/a2a", json=_body()).json()["output"]
    second = client.post("/a2a", json=_body()).json()["output"]
    assert first == second


def test_unlisted_requester_is_rejected(svc, client) -> None:
    res = client.post("/a2a", json=_body(token="mock:stranger")).json()
    assert res["status"] == "rejected"
