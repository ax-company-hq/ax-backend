"""⑤ 예산·한도 정책 테스트: 소진율, 경보, 한도 제안."""

from __future__ import annotations

import pytest

from finance_agent import books as bk
from finance_agent import policy


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, fixed_now) -> None:
    monkeypatch.setenv("AGENT_TEMPLATE_MOCK", "1")


@pytest.mark.parametrize(
    ("ratio", "projected", "expected"),
    [
        (None, False, None),
        (0.2, False, "ok"),
        (0.79, False, "ok"),
        (0.8, False, "warn"),
        (0.2, True, "warn"),
        (1.0, False, "over"),
        (1.4, True, "over"),
    ],
)
def test_alert_levels(ratio, projected, expected) -> None:
    assert policy._alert(ratio, projected) == expected


def test_percentile_nearest_rank() -> None:
    assert policy.percentile([], 0.95) is None
    assert policy.percentile([5, 1, 3], 0.5) == 3
    assert policy.percentile(list(range(1, 101)), 0.95) == 95


def test_seed_policy_rules_and_burn_rate() -> None:
    data, missing = policy.policy_report(bk.seed_books())
    assert missing == [] and data["how_to_change"].startswith("기준표")
    rules = {r["key"]: r for r in data["rules"]}
    assert {
        "coupon_total",
        "coupon_unit",
        "coupon_daily",
        "refund_single",
        "seller_risk",
        "compensation_per_customer",
        "coupon_budget_month",
        "refund_budget_month",
    } <= set(rules)
    assert (rules["refund_single"]["limit"], rules["refund_single"]["unit"]) == (100000, "원")
    assert (rules["seller_risk"]["limit"], rules["seller_risk"]["unit"]) == (70, "점")
    per_request = rules["compensation_per_customer"]
    assert (per_request["used_today"], per_request["ratio"], per_request["alert"]) == (
        None,
        None,
        None,
    )
    daily = rules["coupon_daily"]
    assert daily["used_today"] == 13000 and daily["ratio"] == 0.013 and daily["alert"] == "ok"
    budget = rules["coupon_budget_month"]
    assert budget["source"] == "finance_policy.toml" and budget["limit"] == 300000
    assert budget["ratio"] == round(budget["used_month"] / 300000, 4)
    assert budget["projected_month"] == budget["used_month"] * 31 // 23
    assert budget["alert"] == "warn"
    for r in data["rules"]:
        assert set(r) >= {
            "key",
            "label",
            "limit",
            "unit",
            "used_today",
            "used_month",
            "ratio",
            "alert",
            "source",
        }


def test_refund_limit_proposal_is_clamped_to_step() -> None:
    data, _ = policy.policy_report(bk.seed_books())
    [prop] = [p for p in data["proposals"] if p["key"] == "refund_single"]
    assert (prop["current"], prop["proposed"]) == (100000, 70000)  # p95 31,000 → 4만, 30% 폭 제한
    assert prop["evidence"] == {"sample": 8, "p95": 31000}
    assert "31,000원" in prop["reason"]


def test_step_ratio_and_labels_live_in_finance_policy() -> None:
    """제안 폭은 재무 설정(`finance_policy.toml`)에서 읽는다 (ai_review_policy.toml 아님)."""
    from finance_agent import finance_config as fc

    assert fc.load_config().proposal.max_step_ratio == 0.3
    assert policy.KEY_KO["refund_single"] == "환불 한 건"
    data, _ = policy.policy_report(bk.seed_books())
    labels = {r["key"]: r["label"] for r in data["rules"]}
    assert labels.get("refund_single") == "환불 한 건"


def test_no_proposal_without_enough_samples(kit) -> None:
    refunds = [
        {"refund_id": f"rfd_{i}", "amount": 1000, "created_at": kit.at("2026-10-22")}
        for i in range(4)
    ]
    assert policy.proposals(kit.books(refunds=refunds)) == []
    big = refunds + [{"refund_id": "rfd_9", "amount": 95000, "created_at": kit.at("2026-10-22")}]
    assert policy.proposals(kit.books(refunds=big)) == []  # p95 95,000 → 10만 = 지금 값
    small = refunds + [{"refund_id": "rfd_8", "amount": 1000, "created_at": kit.at("2026-10-22")}]
    [prop] = policy.proposals(kit.books(refunds=small))
    assert (prop["key"], prop["proposed"]) == ("refund_single", 70000)
    old = [r | {"created_at": kit.at("2026-10-01")} for r in small]  # 14일 밖
    assert policy.proposals(kit.books(refunds=old)) == []


def test_thresholds_unavailable(kit) -> None:
    data, missing = policy.policy_report(kit.books(thresholds=None))
    assert missing == ["thresholds"]
    assert [r["key"] for r in data["rules"]] == ["coupon_budget_month", "refund_budget_month"]
    assert data["proposals"] == []
