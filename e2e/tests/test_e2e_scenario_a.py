"""e2e 틀 자체의 테스트: 단계 표, 상태 판정, 첫 실행 기준선, real 스텁 (부서 앱은 띄우지 않음)."""

import asyncio
from datetime import UTC, datetime

import pytest
from ax_e2e.scenario_a import customer, judge
from ax_e2e.scenario_a.once import run_once
from ax_e2e.scenario_a.result import Outcome
from ax_e2e.scenario_a.stages import CUSTOMER, INCIDENT, build_stage_table

from ax_contracts.data_access.dev_status import DevStatus, Stage, StageResult

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def test_stage_table_is_generated_from_plan() -> None:
    from ax_orchestrator.plans.scenario_a import scenario_a

    stages = build_stage_table()
    plan_ids = [s.id for s in scenario_a().steps]
    assert [s.key for s in stages if s.path == INCIDENT] == plan_ids
    assert len([s for s in stages if s.path == INCIDENT]) == 12
    assert len([s for s in stages if s.path == CUSTOMER]) == 5
    assert [s.no for s in stages] == list(range(1, 18))
    by_key = {s.key: s for s in stages}
    assert by_key["impact"].dept == "data" and by_key["impact"].req_ids == ["DEP-DATA-02"]
    assert by_key["diagnose"].dept == "infra"  # 카드에 skill이 없어도 예정 담당을 표시한다
    assert all(s.condition for s in stages)


def raw(state: str, **kw) -> Outcome:
    return Outcome(state, **kw)  # type: ignore[arg-type]


def result(state: str, commit: str | None = None) -> StageResult:
    return StageResult(state=state, since=NOW, last_pass_commit=commit)  # type: ignore[arg-type]


def test_merge_runs_requires_all_three_identical() -> None:
    assert judge.merge_runs([raw("pass")] * 3).state == "pass"
    mixed = judge.merge_runs([raw("pass"), raw("fail"), raw("pass")])
    assert mixed.state == "fail" and "flaky" in mixed.note


@pytest.mark.parametrize(
    ("outcome", "prev", "expected"),
    [
        ("pass", None, "pass"),
        ("fail", None, "broken"),  # 첫 실행에 실패: 통과 이력 없음
        ("absent", None, "pending"),
        ("blocked", None, "pending"),
        ("fail", result("pass", "abc"), "regressed"),
        ("absent", result("pass", "abc"), "regressed"),  # skill이 사라짐
        ("fail", result("regressed", "abc"), "regressed"),  # 계속 실패
        ("fail", result("broken"), "broken"),
        ("pass", result("regressed", "abc"), "pass"),
        ("pending", result("pending"), "pending"),
    ],
)
def test_state_transitions(outcome: str, prev: StageResult | None, expected: str) -> None:
    got = judge.to_result(raw(outcome), prev, "def", NOW)
    assert got.state == expected
    if expected == "pass":
        assert got.last_pass_commit == "def"
    elif prev is not None:
        assert got.last_pass_commit == prev.last_pass_commit


def make_runs(state_by_key: dict[str, str]) -> list[dict]:
    stages = build_stage_table()
    one = {f"{s.path}:{s.key}": raw(state_by_key.get(s.key, "pass")).to_json() for s in stages}
    return [{"meta": {}, "unit": one, "chain": one} for _ in range(3)]


def test_first_run_is_baseline_and_never_regressed() -> None:
    stages = build_stage_table()
    status, detail = judge.judge(stages, make_runs({"impact": "fail"}), None, "c1", NOW)
    assert detail["baseline"] is True
    assert judge.regressions(status) == []
    assert status.totals.unit_pass == 16 and status.totals.total == 17
    assert next(s for s in status.stages if "impact" in s.name).unit.state == "broken"
    assert detail["paths"]["incident"] == {"total": 12, "unit_pass": 11, "chain_pass": 11}


def prev_all_pass(stages) -> DevStatus:
    status, _ = judge.judge(stages, make_runs({}), None, "c0", NOW)
    return status


def test_only_pass_to_fail_is_regressed_and_blocked_is_not_a_cause() -> None:
    stages = build_stage_table()
    prev = prev_all_pass(stages)
    runs = make_runs({"impact": "fail", "loss": "blocked", "record": "pass"})
    status, detail = judge.judge(stages, runs, prev, "c1", NOW)
    found = judge.regressions(status)
    names = {f["name"] for f in found}
    assert any("impact" in n for n in names) and not any("loss" in n for n in names)
    assert {f["view"] for f in found} == {"unit", "chain"}
    impact = next(s for s in status.stages if "impact" in s.name)
    assert impact.unit.state == "regressed" and impact.unit.last_pass_commit == "c0"
    assert detail["baseline"] is False


def test_unchanged_failure_keeps_since_and_issue() -> None:
    stages = build_stage_table()
    prev = prev_all_pass(stages)
    first, _ = judge.judge(stages, make_runs({"impact": "fail"}), prev, "c1", NOW)
    impact = next(s for s in first.stages if "impact" in s.name)
    patched = first.model_copy(
        update={
            "stages": [
                Stage.model_validate(
                    {**s.model_dump(), "unit": {**s.unit.model_dump(), "issue": 77}}
                )
                if s is impact
                else s
                for s in first.stages
            ]
        }
    )
    later = datetime(2026, 10, 24, tzinfo=UTC)
    second, _ = judge.judge(stages, make_runs({"impact": "fail"}), patched, "c2", later)
    again = next(s for s in second.stages if "impact" in s.name)
    assert again.unit.state == "regressed" and again.unit.issue == 77
    assert again.unit.since == NOW  # 상태가 안 바뀌면 처음 시각을 유지


def test_customer_chain_is_pending_until_support_can_link_incident_context() -> None:
    stages = build_stage_table()
    out = asyncio.run(customer.run_chain(stages, None, "ntc_0001", "ctx_inc_x"))  # type: ignore[arg-type]
    assert set(out) == {s.key for s in stages if s.path == CUSTOMER}
    assert all(o.state == "pending" and "blocked-by #272 #274" in o.note for o in out.values())


def test_real_mode_stub_marks_everything_pending() -> None:
    out = run_once("real")
    assert {v["state"] for v in out["chain"].values()} == {"absent"}
    assert {v["state"] for v in out["unit"].values()} == {"absent"}


def test_already_regressed_stage_is_not_a_new_regression() -> None:
    stages = build_stage_table()
    prev = prev_all_pass(stages)
    runs = make_runs({"impact": "fail"})
    first, _ = judge.judge(stages, runs, prev, "c1", NOW)
    assert judge.regressions(first, prev)  # 처음 실패한 실행: 새 회귀
    second, _ = judge.judge(stages, runs, first, "c2", NOW)
    assert next(s for s in second.stages if "impact" in s.name).unit.state == "regressed"
    assert judge.regressions(second, first) == []  # 계속 실패 중: 이슈·revert 대상이 아니다
    assert judge.regressions(second) != []  # prev를 안 주면 상태만 본다


# ---- 쿠폰 등급 (#319 재무 재설계: 쿠폰 A, 재무 verify_amount 호출 없음)


def test_customer_coupon_stage_is_grade_a_without_finance() -> None:
    stages = build_stage_table()
    cust = {s.key: s for s in stages if s.path == CUSTOMER}
    grade = cust["coupon_grade"]
    assert grade.dept == "safety" and grade.skill == "issue_coupon"
    assert "verify_amount" not in [s.skill for s in stages]
    assert "finance_check" not in cust


def test_coupon_gate_decision_is_a_executed_without_verifier() -> None:
    got, called = asyncio.run(customer.gate_decision("ctx_ui_e2e_test01"))
    assert got == ("A", "executed", None)
    assert called == []  # 재무 verify_amount를 포함해 검증자를 부르지 않는다


class _Log:
    def __init__(self, source: str, kind: str, skill: str | None = None, **kw) -> None:
        self.source, self.kind, self.skill = source, kind, skill
        self.requester = kw.get("requester")
        self.task_status = kw.get("task_status")
        self.payload = kw.get("payload", {})


def test_chain_coupon_grade_reads_common_log() -> None:
    done = _Log("marketing", "result_sent", "issue_coupon", task_status="completed")
    issued = _Log("marketing", "event_published", payload={"event_type": "coupon.issued"})
    held = _Log(
        "marketing",
        "result_sent",
        "issue_coupon",
        task_status="input-required",
        payload={"outcome": "held"},
    )
    verify = _Log("finance", "request_received", "verify_amount", requester="safety")
    assert customer.chain_coupon_grade({}, [done]).state == "pass"
    assert customer.chain_coupon_grade({}, [issued]).state == "pass"
    assert customer.chain_coupon_grade({}, [held]).state == "fail"
    assert customer.chain_coupon_grade({}, [done, verify]).state == "fail"
    assert customer.chain_coupon_grade({}, None).state == "pending"
    blocked = customer.chain_coupon_grade({"judge": Outcome("fail")}, [done])
    assert blocked.state == "blocked" and blocked.blocked_by == ["judge"]
