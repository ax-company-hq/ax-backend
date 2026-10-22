"""결정 탐지 정확도: 한국어 표현 변형 샘플로 결정 여부·이유·기각을 측정한다.

`decision_samples.json`(60개)은 규칙을 만들 때 본 문장이고, `decision_samples_unseen.json`(20개)은
규칙을 정한 뒤 따로 쓴 문장이다. 새 표현을 규칙에 더하면 샘플도 같이 늘린다.
"""

import json
from pathlib import Path

import pytest

from ax_contracts.schemas import RecordCase
from hr_agent.cards.builder import build_card
from hr_agent.cards.decide import detect_decision
from hr_agent.cards.mapping import outcome_of
from hr_agent.config import ManualClock

DATA = Path(__file__).parent / "data"


def _load(name: str) -> list[dict]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _record(sample: dict, **extra) -> RecordCase:
    return RecordCase(
        context_id="ctx_smp_0001",
        kind=sample["kind"],
        summary=sample["summary"],
        author="planning",
        source_refs=[],
        **extra,
    )


def measure(samples: list[dict]) -> dict[str, float]:
    ok = {"decision": 0, "reason": 0, "rejected": 0}
    for s in samples:
        rec = _record(s)
        info = detect_decision(rec)
        ok["decision"] += (info is not None) == s["expect_decision"]
        got = info.reason if info else None
        want = s["expect_reason"]
        ok["reason"] += (got is None) if want is None else (got is not None and want in got)
        rejected = bool(info and (info.rejected or outcome_of(rec.kind) == "rejected"))
        ok["rejected"] += rejected == s["expect_rejected"]
    return {k: v / len(samples) for k, v in ok.items()}


def test_samples_seen_while_building_the_rules() -> None:
    samples = _load("decision_samples.json")
    assert len(samples) == 68
    assert measure(samples) == {"decision": 1.0, "reason": 1.0, "rejected": 1.0}


def test_samples_written_after_the_rules() -> None:
    samples = _load("decision_samples_unseen.json")
    got = measure(samples)
    assert all(v >= 0.9 for v in got.values()), got


@pytest.mark.parametrize(
    "summary",
    [
        "기각하지 않고 조건부로 승인했다",
        "기존 반려 결정을 철회하고 도입하기로 했다",
        "기각 건수가 줄었다",
    ],
)
def test_negated_or_withdrawn_rejections_are_not_read_as_rejections(summary: str) -> None:
    info = detect_decision(
        RecordCase(context_id="ctx_x_0001", kind="decision", summary=summary, author="p")
    )
    assert info is not None and info.rejected is False


def test_noun_use_of_wihae_is_not_a_reason() -> None:
    info = detect_decision(
        RecordCase(
            context_id="ctx_x_0001", kind="decision", summary="위해 요소를 제거한다", author="p"
        )
    )
    assert info is not None and info.reason is None


def test_non_decision_kinds_never_get_a_reason_from_the_sentence() -> None:
    rec = RecordCase(
        context_id="ctx_x_0001", kind="notice", summary="점검 때문에 서비스가 중단된다", author="p"
    )
    assert detect_decision(rec) is None


def test_a_rejection_read_from_the_sentence_is_a_candidate_not_a_confirmed_card() -> None:
    rec = RecordCase(
        context_id="ctx_x_0002",
        kind="decision",
        summary="결제 서버 캐시 도입안을 기각했다",
        decision_reason="정합성 위험",
        alternatives_considered=["현행 유지"],
        source_refs=["req:1", "area:payment"],
        author="planning",
    )
    info = detect_decision(rec)
    card = build_card(rec, info, ManualClock()())
    assert card.extra["outcome"] == "rejected" and card.extra["outcome_inferred"] is True
    assert card.confidence == "mid" and card.extra["candidate"] is True
    assert "rejected" in card.tags


def test_explicit_rejection_kind_stays_high_confidence() -> None:
    rec = RecordCase(
        context_id="ctx_x_0003",
        kind="decision_rejected",
        summary="결제 서버 캐시 도입안을 기각했다",
        decision_reason="정합성 위험",
        alternatives_considered=["현행 유지"],
        source_refs=["req:1", "area:payment"],
        author="planning",
    )
    card = build_card(rec, detect_decision(rec), ManualClock()())
    assert card.confidence == "high" and card.extra["outcome_inferred"] is False
