"""보류 큐 테스트: 사람 결정, 기한(데모 10분·운영 24시간), 기한 지나면 자동 거절."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from ax_contracts.schemas import HoldReason
from ax_safety.gate import Gate
from ax_safety.rules import load_registry, load_thresholds
from ax_safety.store import Store


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
    """기본 기준표(데모)는 10분: 599초까지는 대기, 600초에 자동 거절."""
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="slow"))
    clock.advance(599)
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


@pytest.fixture
def ops_gate(monkeypatch: pytest.MonkeyPatch, clock: Any, verifier: Any, sent: Any) -> Gate:
    """운영 설정: `SAFETY_REVIEW_TIMEOUT_SECONDS=86400`(24시간)으로 띄운 관문."""
    monkeypatch.setenv("SAFETY_REVIEW_TIMEOUT_SECONDS", "86400")
    return Gate(load_registry(), load_thresholds(), Store(), verifier, sent, now=clock)


@pytest.mark.asyncio
async def test_ops_review_is_still_pending_after_ten_minutes(
    ops_gate: Gate, clock: Any, coupon
) -> None:
    """운영 기한이 24시간이면 10분·12시간 뒤에도 아직 사람 결정을 기다린다."""
    held = await ops_gate.execute(coupon(20, 20_000, purpose_code="campaign", key="wait"))
    review = ops_gate.store.get_review(held.review_id or "")
    assert review is not None
    assert review.expires_at - review.created_at == timedelta(hours=24)
    for seconds in (600, 12 * 3600, 24 * 3600 - 1):
        clock.now = review.created_at + timedelta(seconds=seconds)
        assert await ops_gate.expire_reviews() == []
    assert ops_gate.store.get_review(held.review_id or "").status == "pending"  # type: ignore[union-attr]
    clock.now = review.created_at + timedelta(hours=24)
    assert [r.review_id for r in await ops_gate.expire_reviews()] == [held.review_id]


@pytest.mark.asyncio
async def test_model_copy_with_24h_deadline(clock: Any, verifier: Any, sent: Any, coupon) -> None:
    """기준표 복사본으로 24시간을 줘도 같은 기한이 잡힌다(환경변수 없이)."""
    table = load_thresholds().model_copy(update={"review_timeout_seconds": 86_400})
    gate = Gate(load_registry(), table, Store(), verifier, sent, now=clock)
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="copy"))
    review = gate.store.get_review(held.review_id or "")
    assert review is not None
    assert review.expires_at - review.created_at == timedelta(hours=24)


@pytest.mark.asyncio
async def test_decision_after_expiry_is_refused(gate: Gate, clock: Any, coupon) -> None:
    held = await gate.execute(coupon(20, 20_000, purpose_code="campaign", key="late"))
    clock.advance(600)
    with pytest.raises(ValueError, match="expired"):
        await gate.decide_review(held.review_id or "", "approved", "태윤", None)
