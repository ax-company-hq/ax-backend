"""규칙 파일 테스트: 등록부·기준표 로딩, 합의 값, 규칙 키 중복·보호 경로 규칙, 자기 검증 금지
(SAF-01)."""

from __future__ import annotations

from typing import Any

import pytest

from ax_safety.gate import Gate
from ax_safety.main import create_app
from ax_safety.rules import ActionSpec, Rule, ThresholdTable, load_registry, load_thresholds


def test_rule_files_load_and_match_agreed_values() -> None:
    registry, table = load_registry(), load_thresholds()
    assert table.version == 1
    assert table.review_timeout_seconds == 600  # 데모 10분 자동 거절
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


def test_review_timeout_env_overrides_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """운영은 `SAFETY_REVIEW_TIMEOUT_SECONDS`로 사람 확인 기한을 24시간으로 덮어쓴다."""
    monkeypatch.setenv("SAFETY_REVIEW_TIMEOUT_SECONDS", "86400")
    table = load_thresholds()
    assert table.review_timeout_seconds == 86_400
    assert table.verify_timeout_seconds == load_thresholds().verify_timeout_seconds  # 나머지 그대로
    monkeypatch.setenv("SAFETY_REVIEW_TIMEOUT_SECONDS", "")  # 비어 있으면 기준표 값
    assert load_thresholds().review_timeout_seconds == 600


@pytest.mark.parametrize("raw", ["abc", "1.5", "59", "604801", "-600", "0"])
def test_invalid_review_timeout_env_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SAFETY_REVIEW_TIMEOUT_SECONDS", raw)
    with pytest.raises(ValueError, match="SAFETY_REVIEW_TIMEOUT_SECONDS"):
        load_thresholds()
    with pytest.raises(ValueError, match="SAFETY_REVIEW_TIMEOUT_SECONDS"):
        create_app(expire_interval=None)  # 서버 시작 단계에서 바로 멈춘다


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


def test_on_verify_reject_hold_only_on_rollback() -> None:
    """검증자 거절 → 사람 보류는 롤백만. 쿠폰 등은 기본값(reject) 유지 (팀 결정 12)."""
    registry = load_registry()
    holds = {spec.action for spec in registry.actions if spec.on_verify_reject == "hold"}
    assert holds == {"rollback_deployment"}
    assert registry.get("issue_coupon").on_verify_reject == "reject"  # type: ignore[union-attr]


def test_on_verify_reject_is_validated() -> None:
    with pytest.raises(ValueError):
        ActionSpec(
            action="x",
            owner="infra",
            base_grade="B",
            verifier="planning",
            verify_skill="v",
            on_verify_reject="queue",
        )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="에이전트 검증자"):  # 검증자 없는 액션에는 못 쓴다
        ActionSpec(action="x", owner="infra", base_grade="A", on_verify_reject="hold")
    with pytest.raises(ValueError, match="에이전트 검증자"):  # 코드 규칙 검증자에도 못 쓴다
        ActionSpec(
            action="x", owner="infra", base_grade="B", verifier="safety", on_verify_reject="hold"
        )
    spec = ActionSpec(
        action="x",
        owner="infra",
        base_grade="B",
        verifier="planning",
        verify_skill="v",
        on_verify_reject="hold",
    )
    assert spec.on_verify_reject == "hold"
