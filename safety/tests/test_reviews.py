"""보류 큐 테스트: 사람 결정, 24시간 기한, 기한 지나면 자동 거절."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate


@pytest.mark.asyncio
async def test_review_approval_turns_replay_into_executed(gate: Gate, sent: Any, coupon) -> None:
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="big"))
    review = await gate.decide_review(held.review_id or "", "approved", "태윤", "확인함")
    assert (review.status, review.decided_by) == ("approved", "태윤")
    replay = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="big"))
    assert (replay.status, replay.grade) == ("executed", "C")
    assert sent.types()[-1] == "safety.review_decided"
    with pytest.raises(ValueError):
        await gate.decide_review(held.review_id or "", "rejected", "태윤", None)


@pytest.mark.asyncio
async def test_review_auto_rejects_after_timeout(gate: Gate, clock: Any, sent: Any, coupon) -> None:
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="slow"))
    clock.advance(24 * 3600 - 1)
    assert await gate.expire_reviews() == []
    clock.advance(1)
    expired = await gate.expire_reviews()
    assert [r.review_id for r in expired] == [held.review_id]
    replay = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="slow"))
    assert (replay.status, replay.hold_reason) == ("rejected", HoldReason.REVIEW_TIMEOUT)
    event_type, data = sent.sent[-1]
    assert (event_type, data["decision"], data["decided_by"]) == (
        "safety.review_decided",
        "rejected",
        "system",
    )


@pytest.mark.asyncio
async def test_review_is_still_pending_after_ten_minutes(gate: Gate, clock: Any, coupon) -> None:
    """기한이 24시간이라 10분·12시간 뒤에는 아직 사람 결정을 기다린다."""
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="wait"))
    review = gate.store.get_review(held.review_id or "")
    assert review is not None
    assert review.expires_at - review.created_at == timedelta(hours=24)
    for seconds in (600, 12 * 3600):
        clock.now = review.created_at + timedelta(seconds=seconds)
        assert await gate.expire_reviews() == []
    assert gate.store.get_review(held.review_id or "").status == "pending"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_decision_after_expiry_is_refused(gate: Gate, clock: Any, coupon) -> None:
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="late"))
    clock.advance(24 * 3600)
    with pytest.raises(ValueError, match="expired"):
        await gate.decide_review(held.review_id or "", "approved", "태윤", None)
