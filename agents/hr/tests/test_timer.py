"""교대 시각 점검: 곧 끝나는 교대의 인수인계를 자동으로 시작한다."""

import pytest

from hr_agent.roster.timer import start_due_handovers, upcoming_handoffs


def test_only_shifts_with_a_successor_are_handoffs(svc) -> None:
    found = upcoming_handoffs(svc, horizon_minutes=780)
    pairs = {(h.area, h.from_member, h.to_member) for h in found}
    assert ("payment", "emp_01", "emp_02") in pairs
    assert ("infra-ops", "emp_08", "emp_09") in pairs
    assert not any(h.area == "catalog" for h in found)  # 이어받는 사람이 없다 → 공백으로 따로 알림


def test_only_the_boundary_inside_the_horizon_is_due(svc) -> None:
    # 시계는 09:00이고 아침 교대가 막 바뀌는 시각이다. 저녁 교대(18시 이후)는 아직 먼 일이다.
    due = upcoming_handoffs(svc, horizon_minutes=30)
    assert due and all(h.at == svc.clock() for h in due)
    assert not any(h.at.hour == 18 for h in due)


@pytest.mark.asyncio
async def test_due_handovers_start_once_per_shift(svc) -> None:
    first = await start_due_handovers(svc, horizon_minutes=780)
    assert first and all(r["handover_id"] for r in first)
    count = len(svc.store.all("SELECT * FROM handovers"))
    assert count == len(first)
    again = await start_due_handovers(svc, horizon_minutes=780)
    assert [r["handover_id"] for r in again] == [r["handover_id"] for r in first]
    assert len(svc.store.all("SELECT * FROM handovers")) == count  # 같은 교대에 두 번 만들지 않는다
    row = svc.store.one(
        "SELECT reason, context_id FROM handovers WHERE area = 'payment' AND owner_id = 'emp_01'"
    )
    assert row["reason"] == "shift" and row["context_id"].startswith("ctx_hnd_shift_payment_")


@pytest.mark.asyncio
async def test_time_moving_forward_picks_up_the_next_boundary(svc) -> None:
    svc.clock.advance(hours=10)  # 19:00 → 다음 교대 끝(내일 09:00)이 24h 안에 들어온다
    later = upcoming_handoffs(svc, horizon_minutes=24 * 60)
    assert any(h.area == "payment" and h.from_member == "emp_02" for h in later)
