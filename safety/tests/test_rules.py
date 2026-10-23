"""규칙 파일 테스트: 등록부·기준표 로딩, 합의 값, 규칙 키 중복·보호 경로 규칙, 자기 검증 금지
(SAF-01)."""

from __future__ import annotations

from typing import Any

import pytest

from ax_safety.gate import Gate
from ax_safety.rules import ActionSpec, Rule, ThresholdTable, load_registry, load_thresholds


def test_rule_files_load_and_match_agreed_values() -> None:
    registry, table = load_registry(), load_thresholds()
    assert table.version == 1
    assert table.review_timeout_seconds == 24 * 3600  # 사람 확인 기한 24시간
    values = {rule.key: rule.value for rule in table.rules if rule.metric != "param"}
    values.pop("pr_allowed_paths")
    values.pop("merge_allowed_paths")
    values.pop("pr_protected_paths")
    values.pop("merge_protected_paths")
    assert values == {
        "coupon_total": 300_000,
        "coupon_unit": 20_000,
        "coupon_daily": 1_000_000,
        "compensation_per_context": 1_000_000,
        "compensation_per_customer": 50_000,
        "refund_single": 100_000,
    }
    issue = registry.get("issue_coupon")
    assert issue is not None and (issue.base_grade, issue.verifier) == ("B", "finance")
    assert registry.get("confirm_settlement").base_grade == "C"  # type: ignore[union-attr]
    for spec in registry.actions:  # 자기 검증 금지
        assert spec.verifier != spec.owner


def test_duplicate_rule_keys_are_refused() -> None:
    rule = {"key": "dup", "action": "x", "metric": "amount", "value": 1}
    with pytest.raises(ValueError, match="같은 규칙 키가 두 번 있다: dup"):
        ThresholdTable.model_validate(
            {
                "version": 1,
                "verify_timeout_seconds": 30,
                "review_timeout_seconds": 600,
                "rules": [rule, {**rule, "action": "y"}],
            }
        )


def test_self_verification_is_refused_at_registration() -> None:
    with pytest.raises(ValueError, match="자기 검증"):
        ActionSpec(
            action="x", owner="finance", base_grade="B", verifier="finance", verify_skill="v"
        )
    with pytest.raises(ValueError, match="verifier"):
        ActionSpec(action="x", owner="marketing", base_grade="B")
    with pytest.raises(ValueError, match="자기 검증"):  # 요청자 목록에 검증자가 있어도 금지
        ActionSpec(
            action="x",
            owner="memory",
            requesters=["hr", "data"],
            base_grade="B",
            verifier="data",
            verify_skill="v",
        )


def test_threshold_rules_must_reference_registered_actions(verifier: Any, sent: Any) -> None:
    table = load_thresholds()
    bad = table.model_copy(
        update={"rules": [*table.rules, table.rules[0].model_copy(update={"action": "ghost"})]}
    )
    from ax_safety.store import Store

    with pytest.raises(ValueError, match="ghost"):
        Gate(load_registry(), bad, Store(), verifier, sent)


def test_protected_paths_rules_are_loaded_for_both_actions() -> None:
    """#169: 보호 경로는 generate_pr·merge_pr에 같은 목록으로 걸려 있다."""
    table = load_thresholds()
    rules = {rule.key: rule for rule in table.rules if rule.metric == "paths_avoid"}
    assert set(rules) == {"pr_protected_paths", "merge_protected_paths"}
    pr, merge = rules["pr_protected_paths"], rules["merge_protected_paths"]
    assert (pr.action, merge.action) == ("generate_pr", "merge_pr")
    assert pr.field == merge.field == "changed_paths"
    assert pr.protected == merge.protected
    for path in ("shop-web/src/pages/Checkout.tsx", "shop-api/src/ax_shop_api/service.py"):
        assert path in pr.protected


def test_paths_avoid_rule_needs_protected() -> None:
    base = {"key": "p", "action": "generate_pr", "metric": "paths_avoid", "field": "changed_paths"}
    with pytest.raises(ValueError, match="protected가 필요하다"):
        Rule.model_validate(base)
    with pytest.raises(ValueError, match="protected가 필요하다"):
        Rule.model_validate({**base, "protected": [""]})  # 빈 접두사는 모든 경로와 맞으므로 금지
    with pytest.raises(ValueError, match="field가 필요하다"):
        Rule.model_validate(
            {**{k: v for k, v in base.items() if k != "field"}, "protected": ["a/"]}
        )
    rule = Rule.model_validate({**base, "protected": ["shop-web/src/lib/auth.tsx", ".github/"]})
    assert rule.paths_protected(
        ["shop-web/src/lib/auth.tsx", ".github/workflows/ci.yml", "shop-web/src/pages/Home.tsx"]
    ) == ["shop-web/src/lib/auth.tsx", ".github/workflows/ci.yml"]
